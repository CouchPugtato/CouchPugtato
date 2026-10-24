"""Render a JSON profile into a single-play GIF and a static PNG."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import json
import heapq
import math
import os
import random
import re
from pathlib import Path
from urllib.request import Request, urlopen

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SIZE = (1100, 440)
BG, PANEL, INK, MUTED = "#08060d", "#1d142e", "#f5edff", "#bdabcd"
SECONDARY = "#ff9a52"
TERRAIN = "#4493f8"
TIMELINE_SECONDS = 12.5


@lru_cache(maxsize=64)
def font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in (
        "DejaVuSansMono.ttf", "C:/Windows/Fonts/consola.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/System/Library/Fonts/Menlo.ttc",
    ):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    raise ValueError("No supported font found. Install DejaVu Sans Mono or edit font().")


def text(draw, xy, value, size, width, max_lines, fill=INK):
    face = font(size)
    lines, current = [], ""
    for word in value.split():
        if draw.textlength(word, font=face) > width:
            raise ValueError(f"Word is too wide for the layout: {word!r}")
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=face) <= width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        raise ValueError(f"Text needs {len(lines)} lines; this field allows {max_lines}: {value!r}")
    for index, line in enumerate(lines):
        draw.text((xy[0], xy[1] + index * (size + 7)), line, font=face, fill=fill)


def read_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Config must be a JSON object")
    for key in ("name", "eyebrow", "description", "footer", "accent"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f"{key} must be a nonempty string")
    ImageColor.getrgb(config["accent"])
    if "linkedin_url" in config and (
            not isinstance(config["linkedin_url"], str) or
            not config["linkedin_url"].startswith("https://")):
        raise ValueError("linkedin_url must be an HTTPS URL")
    for key, default, low, high in (("fps", 15, 5, 25), ("duration_seconds", 4, 1, 16)):
        value = config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ValueError(f"{key} must be between {low} and {high}")
        config[key] = value
    if not float(config["fps"]).is_integer():
        raise ValueError("fps must be a whole number")
    stats = config.get("stats", [])
    if not isinstance(stats, list) or len(stats) > 4:
        raise ValueError("stats must be a list with at most four entries")
    for stat in stats:
        if not isinstance(stat, dict) or any(
                not isinstance(stat.get(key), str) or not stat[key].strip()
                for key in ("value", "label")):
            raise ValueError("Each statistic needs a nonempty value and label")
    languages = config.get("languages", {})
    if not isinstance(languages, dict) or any(
            not isinstance(name, str) or not isinstance(count, int) or isinstance(count, bool) or count < 0
            for name, count in languages.items()):
        raise ValueError("languages must map language names to nonnegative byte counts")
    return config


def github_api(path):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "CouchPugtato-profile"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(f"https://api.github.com{path}", headers=headers)
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def refresh_github_stats(config_path):
    """Save current public GitHub counts only after all API requests succeed."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    username = config.get("github_username", "")
    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", username):
        raise ValueError("github_username must be a valid GitHub username")
    user = github_api(f"/users/{username}")
    stars, page, owned_repos, repository_details = 0, 1, [], {}
    while True:
        repos = github_api(f"/users/{username}/repos?type=owner&per_page=100&page={page}")
        stars += sum(repo["stargazers_count"] for repo in repos if not repo.get("private", False))
        owned_repos.extend(repo for repo in repos if not repo.get("private", False) and not repo.get("fork", False))
        for repo in repos:
            if not repo.get("private", False) and repo.get("full_name"):
                language = repo.get("language") or ""
                repository_details[repo["full_name"].casefold()] = {
                    "language": "JavaScript" if language in ("CSS", "HTML") else language}
        if len(repos) < 100:
            break
        page += 1
    paths = [f"/repos/{repo['full_name']}/languages" for repo in owned_repos]
    with ThreadPoolExecutor(max_workers=6) as executor:
        languages, language_repos = aggregate_languages(executor.map(github_api, paths))
    config["languages"] = dict(sorted(languages.items(), key=lambda item: (-item[1], item[0])))
    config["language_repos"] = dict(sorted(language_repos.items(), key=lambda item: (-item[1], item[0])))
    config["repository_details"] = repository_details
    # search counts public commits authored by the user across indexed repositories
    commits = github_commit_count(username)
    config["weekly_activity"], config["monthly_repositories"] = github_weekly_activity(username, include_monthly=True)
    config["stats"] = [
        {"value": f"{commits:,}", "label": "COMMITS"},
        {"value": f"{user['followers']:,}", "label": "FOLLOWERS"},
        {"value": f"{user['public_repos']:,}", "label": "PUBLIC REPOSITORIES"},
        {"value": f"{stars:,}", "label": "REPOSITORY STARS"},
    ]
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config["stats"]


def aggregate_languages(repository_languages):
    """Group web markup/styles with JavaScript, counting each repository once."""
    languages, language_repos = {}, {}
    for result in repository_languages:
        seen = set()
        for name, count in result.items():
            name = "JavaScript" if name in ("JavaScript", "CSS", "HTML") else name
            languages[name] = languages.get(name, 0) + count
            if count > 0:
                seen.add(name)
        for name in seen:
            language_repos[name] = language_repos.get(name, 0) + 1
    return languages, language_repos


def github_commit_count(username):
    result = github_api(f"/search/commits?q=author%3A{username}+is%3Apublic&per_page=1")
    if result.get("incomplete_results"):
        raise ValueError("GitHub returned an incomplete commit count; saved stats were not changed")
    return result["total_count"]


def github_weekly_activity(username, today=None, include_monthly=False):
    """Aggregate weekly commits and optional six-month repository breadth from one search."""
    today = today or datetime.now(timezone.utc).date()
    monday = today - timedelta(days=today.weekday())
    first = monday - timedelta(weeks=26)
    last = monday - timedelta(days=1)
    weeks = [{"start": (first + timedelta(weeks=i)).isoformat(), "commits": 0} for i in range(26)]
    month_number = today.year * 12 + today.month - 1
    months = {}
    for index in range(5, -1, -1):
        year, month = divmod(month_number - index, 12)
        months[f"{year:04d}-{month + 1:02d}"] = {}
    query_first = min(first, date.fromisoformat(next(iter(months)) + "-01")) if include_monthly else first
    query_last = today if include_monthly else last

    def fetch_window(start, end):
        query = (f"/search/commits?q=author%3A{username}+is%3Apublic"
                 f"+author-date%3A{start.isoformat()}..{end.isoformat()}"
                 "&sort=author-date&order=asc&per_page=100")
        result = github_api(query + "&page=1")
        if result.get("incomplete_results"):
            raise ValueError("GitHub returned incomplete weekly activity; saved stats were not changed")
        # commit search exposes at most 1,000 results per query split busy periods
        if result["total_count"] > 1000:
            if start == end:
                raise ValueError("More than 1,000 commits on one day; weekly activity was not updated")
            middle = start + (end - start) // 2
            fetch_window(start, middle)
            fetch_window(middle + timedelta(days=1), end)
            return
        pages = (result["total_count"] + 99) // 100
        for page in range(1, max(1, pages) + 1):
            if page > 1:
                result = github_api(query + f"&page={page}")
                if result.get("incomplete_results"):
                    raise ValueError("GitHub returned incomplete weekly activity; saved stats were not changed")
            for item in result["items"]:
                stamp = item["commit"]["author"]["date"]
                day = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc).date()
                if first <= day <= last:
                    weeks[(day - first).days // 7]["commits"] += 1
                month = day.strftime("%Y-%m")
                if include_monthly and month in months and day <= today:
                    name = item["repository"]["full_name"]
                    record = months[month].setdefault(name.casefold(),
                              {"name": name, "last_commit": day.isoformat(), "commits": 0})
                    record["commits"] += 1
                    record["last_commit"] = max(record["last_commit"], day.isoformat())
    fetch_window(query_first, query_last)
    if include_monthly:
        monthly = [{"month": month, "repositories": len(repos),
                    "partial": month == today.strftime("%Y-%m"),
                    "items": sorted(repos.values(), key=lambda repo: (repo["last_commit"], repo["name"]), reverse=True)}
                   for month, repos in months.items()]
        return weeks, monthly
    return weeks


@lru_cache(maxsize=1)
def sun_bands():
    horizon, radius, center = 286, 112, SIZE[0] / 2
    center_y = horizon - 48
    top = center_y - radius
    # gaps grow by two pixels from four to ten toward the horizon
    segments = [(0, 84)]
    cursor, gap = 84, 4
    for height in (18, 16, 10, 4):
        cursor += gap
        segments.append((cursor, cursor + height))
        cursor += height
        gap += 2
    bands = []
    for y in range(top, horizon):
        offset = y - top
        if not any(start <= offset < end for start, end in segments):
            continue
        half_width = math.sqrt(radius ** 2 - (y - center_y) ** 2)
        strength = 0.18 - 0.06 * offset / (horizon - top)
        bands.append((y, round(center - half_width), round(center + half_width), strength))
    return tuple(bands)


@lru_cache(maxsize=1)
def sun_mask():
    mask = Image.new("L", SIZE, 0)
    draw = ImageDraw.Draw(mask)
    for y, left, right, _ in sun_bands():
        draw.line((left, y, right, y), fill=255)
    return mask


@lru_cache(maxsize=1)
def terrain_mesh():
    horizon, center = 286, SIZE[0] / 2
    rng = random.Random(107)
    ridge, middle, base = [], [], []
    for index, x in enumerate(range(-46, SIZE[0] + 47, 46)):
        valley = min(1, max(0, (abs(x - center) - 140) / 100))
        valley = valley * valley * (3 - 2 * valley)
        height = valley * rng.uniform(16, 44)
        ridge.append((x, round(horizon - height)))
        middle.append((x + (7 if index % 2 else -7), round(horizon - height * 0.45)))
        base.append((x, horizon))
    faces = []
    for index in range(len(ridge) - 1):
        for upper, lower in ((ridge, middle), (middle, base)):
            for triangle in ((upper[index], upper[index + 1], lower[index]),
                             (upper[index + 1], lower[index + 1], lower[index])):
                if min(y for _, y in triangle) < horizon - 1:
                    faces.append((triangle, 0.12 + 0.04 * (index % 3)))
    return faces


@lru_cache(maxsize=1)
def terrain_wire_mask():
    mask = Image.new("L", SIZE, 0)
    draw = ImageDraw.Draw(mask)
    for triangle, _ in terrain_mesh():
        draw.polygon(triangle, outline=255)
    center = SIZE[0] // 2
    draw.rectangle((center - 140, 0, center + 140, SIZE[1]), fill=0)
    return mask


@lru_cache(maxsize=1)
def reconstruction_mask():
    return ImageChops.lighter(sun_mask(), terrain_wire_mask())


def terrain_particle_delay(x):
    edge_distance = min(x, SIZE[0] - 1 - x)
    return 0.45 * min(1, max(0, edge_distance / ((SIZE[0] - 1) / 2 - 140)))


@lru_cache(maxsize=8)
def _backdrop_base(accent, show_sun=True, emphasis=0, show_terrain=True):
    image = Image.new("RGB", SIZE, BG)
    draw = ImageDraw.Draw(image)
    background_rgb = ImageColor.getrgb(BG)
    accent_rgb = tuple(round(channel * scale) for channel, scale
                       in zip(ImageColor.getrgb(accent), (0.55, 0.36, 0.65)))
    emphasis = max(0, min(1, emphasis))

    def lit_color(value):
        strength = 0.24 if value == "#1a1125" else 1
        return tuple(round(channel + (bg + (purple - bg) * strength - channel) * emphasis)
                     for bg, purple, channel in zip(background_rgb, accent_rgb, ImageColor.getrgb(value)))

    for y in range(-6, SIZE[1] + 6, 6):
        vertical = (y - SIZE[1] / 2) / (SIZE[1] / 2)
        points = [(x, round(y - 14 * ((x - SIZE[0] / 2) / (SIZE[0] / 2)) ** 4 * vertical))
                  for x in range(0, SIZE[0] + 1, 10)]
        draw.line(points, fill=lit_color("#1a1125"))
    horizon, vanishing_y = 286, 254
    center = SIZE[0] / 2
    # a shallow faceted silhouette sits directly on the floor edge
    terrain_rgb = ImageColor.getrgb(TERRAIN)
    terrain_strength = 0.24 + 0.76 * emphasis
    terrain_faces = []
    edge = tuple(round(bg + (blue - bg) * terrain_strength)
                 for bg, blue in zip(background_rgb, terrain_rgb))
    for triangle, shade in terrain_mesh() if show_terrain else ():
        face = tuple(round(bg + (blue - bg) * terrain_strength * shade)
                     for bg, blue in zip(background_rgb, terrain_rgb))
        terrain_faces.append((triangle, face, edge))
    sun_rgb = ImageColor.getrgb(SECONDARY)
    for y, left, right, strength in sun_bands() if show_sun else ():
        strength += (1 - strength) * emphasis
        color = tuple(round(bg + (sun - bg) * strength)
                      for bg, sun in zip(background_rgb, sun_rgb))
        draw.line((left, y, right, y), fill=color)
    # retain the original floor perspective while extending rays to both edges
    perspective = (horizon - vanishing_y) / (SIZE[1] - vanishing_y)
    ray_count = math.ceil((SIZE[0] / 2) / perspective / 120) + 1
    for index in range(-ray_count, ray_count + 1):
        x = center + index * 120
        start_x = center + (x - center) * perspective
        draw.line((round(start_x), horizon, round(x), SIZE[1]), fill=lit_color("#22162f"))
    for distance in (5, 12, 23, 40, 65, 99, 143):
        y = horizon + distance
        draw.line((0, y, SIZE[0], y), fill=lit_color("#22162f"))
    draw.line((0, horizon, SIZE[0], horizon), fill=lit_color("#22162f"))
    for triangle, face, edge in terrain_faces:
        draw.polygon(triangle, fill=face, outline=edge)
    # curved glass shading grows stronger toward the screen borders
    shading = Image.new("RGBA", SIZE)
    horizontal = [abs((x - SIZE[0] / 2) / (SIZE[0] / 2)) ** 6 for x in range(SIZE[0])]
    shading.putdata([(0, 0, 0, round(min(1, u + abs((y - SIZE[1] / 2) / (SIZE[1] / 2)) ** 6) * 112))
                     for y in range(SIZE[1]) for u in horizontal])
    image = Image.alpha_composite(image.convert("RGBA"), shading).convert("RGB")
    draw = ImageDraw.Draw(image)
    for x, y, sx, sy in ((22, 22, 1, 1), (1077, 22, -1, 1),
                         (22, SIZE[1] - 23, 1, -1), (1077, SIZE[1] - 23, -1, -1)):
        draw.line([(x, y + sy * 26), (x, y), (x + sx * 40, y)], fill=accent, width=2)
    return image


def backdrop(accent, show_sun=True, emphasis=0, show_terrain=True):
    return _backdrop_base(accent, show_sun, emphasis, show_terrain).copy()


@lru_cache(maxsize=1)
def arm_mesh():
    """An industrial manipulator: pedestal, three links, joint barrels, and gripper."""
    vertices, edges = [], []
    joint_contacts = {}

    def shape(points, connections, secondary=False):
        offset = len(vertices)
        vertices.extend(points)
        edges.extend((offset + a, offset + b, secondary) for a, b in connections)

    def barrel(center, radius, length, axis="z", sides=10, material=True):
        ring = [(radius * math.cos(math.tau * i / sides),
                 radius * math.sin(math.tau * i / sides)) for i in range(sides)]
        if axis == "z":
            ring.extend(joint_contacts.get(center, []))
            ring = list({(round(x, 8), round(y, 8)): (x, y) for x, y in ring}.values())
            ring.sort(key=lambda point: math.atan2(point[1], point[0]) % math.tau)
        count = len(ring)
        points = []
        for end in (-length / 2, length / 2):
            for u, v in ring:
                delta = (u, end, v) if axis == "y" else (u, v, end)
                points.append(tuple(center[j] + delta[j] for j in range(3)))
        connections = [(i, (i + 1) % count) for i in range(count)]
        connections += [(i + count, (i + 1) % count + count) for i in range(count)]
        supports = {(round(radius * math.cos(math.tau * i / sides), 8),
                     round(radius * math.sin(math.tau * i / sides), 8)) for i in range(0, sides, 2)}
        connections += [(i, i + count) for i, (u, v) in enumerate(ring)
                        if (round(u, 8), round(v, 8)) in supports]
        shape(points, connections, material)

    def joint_profile(center, direction, normal, width, joint):
        radius, length = joint
        polygon = [(radius * math.cos(math.tau * i / 10),
                    radius * math.sin(math.tau * i / 10)) for i in range(10)]
        contacts = []
        for side in (1, -1):
            origin = (normal[0] * width * side, normal[1] * width * side)
            intersections = []
            for a, b in zip(polygon, polygon[1:] + polygon[:1]):
                edge = (b[0] - a[0], b[1] - a[1])
                delta = (a[0] - origin[0], a[1] - origin[1])
                denominator = direction[0] * edge[1] - direction[1] * edge[0]
                if abs(denominator) < 1e-9:
                    continue
                distance = (delta[0] * edge[1] - delta[1] * edge[0]) / denominator
                fraction = (delta[0] * direction[1] - delta[1] * direction[0]) / denominator
                if distance >= 0 and -1e-9 <= fraction <= 1 + 1e-9:
                    intersections.append(distance)
            distance = min(intersections)
            contact = (origin[0] + distance * direction[0], origin[1] + distance * direction[1])
            joint_contacts.setdefault(center, []).append(contact)
            contacts.append((center[0] + contact[0], center[1] + contact[1]))
        plus, minus = contacts
        return [(plus[0], plus[1], center[2] - length / 2),
                (minus[0], minus[1], center[2] - length / 2),
                (minus[0], minus[1], center[2] + length / 2),
                (plus[0], plus[1], center[2] + length / 2)]

    def beam(start, end, width, depth, material="terrain", brace_material=None,
             start_joint=None, end_joint=None):
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        nx, ny = -dy / length * width, dx / length * width
        points = []
        direction = (dx / length, dy / length)
        normal = (-direction[1], direction[0])
        for center, joint, outward in ((start, start_joint, direction),
                                       (end, end_joint, (-direction[0], -direction[1]))):
            if joint is not None:
                points.extend(joint_profile(center, outward, normal, width, joint))
            else:
                x, y, z = center
                points.extend([(x + nx, y + ny, z - depth), (x - nx, y - ny, z - depth),
                               (x - nx, y - ny, z + depth), (x + nx, y + ny, z + depth)])
        shape(points, [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6),
                       (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7)], material)
        shape([points[0], points[5], points[3], points[6]], [(0, 1), (2, 3)],
              material if brace_material is None else brace_material)

    barrel((-20, -96, 0), 44, 15, "y", 8)
    barrel((-20, -73, 0), 23, 32, "y", 8, material="terrain")
    shoulder, elbow, wrist, tool = (-20, -48, 0), (-56, 38, 0), (38, 82, 0), (68, 57, 0)
    beam(shoulder, elbow, 12, 13, start_joint=(18, 36), end_joint=(17, 32))
    beam(elbow, wrist, 10, 10, start_joint=(17, 32), end_joint=(12, 25))
    beam(wrist, tool, 8, 8, brace_material="terrain", start_joint=(12, 25), end_joint=(10, 28))
    for center, radius, length in ((shoulder, 18, 36), (elbow, 17, 32), (wrist, 12, 25)):
        barrel(center, radius, length)
    barrel(tool, 10, 28, material="terrain")
    # two offset jaws form a physical parallel gripper, rather than fingers on a hand
    for z in (-17, 17):
        beam((68, 57, z), (89, 39, z), 4, 3, brace_material="terrain")
        beam((89, 39, z), (98, 31, z * 0.55), 3, 3, brace_material="terrain")
    return vertices, edges


@lru_cache(maxsize=1)
def arm_drawing_graph():
    """Join the mesh into one network and measure wire distance from its base seed."""
    original, original_edges = arm_mesh()
    vertices, lookup, indices = [], {}, []
    for point in original:
        key = tuple(round(value, 6) for value in point)
        if key not in lookup:
            lookup[key] = len(vertices)
            vertices.append(point)
        indices.append(lookup[key])
    edges = [(indices[a], indices[b], color) for a, b, color in original_edges]
    root = len(vertices)
    vertices.append((-20, -103.5, 0))
    edges.append((root, 0, True))
    adjacency = [[] for _ in vertices]

    def connect(a, b):
        distance = math.dist(vertices[a], vertices[b])
        adjacency[a].append((b, distance))
        adjacency[b].append((a, distance))

    for a, b, _ in edges:
        connect(a, b)
    # nearby components meet through short structural wires, so nothing appears in isolation
    reached = set()
    pending = [root]
    while True:
        while pending:
            node = pending.pop()
            if node not in reached:
                reached.add(node)
                pending.extend(neighbor for neighbor, _ in adjacency[node])
        outside = set(range(len(vertices))) - reached
        if not outside:
            break
        a, b = min(((a, b) for a in reached for b in outside),
                   key=lambda pair: math.dist(vertices[pair[0]], vertices[pair[1]]))
        # the short attachment within the pedestal shares its orange material
        material = True if max(vertices[a][1], vertices[b][1]) <= -88.5 else "terrain"
        edges.append((a, b, material))
        connect(a, b)
        pending.append(b)
    distances = [math.inf] * len(vertices)
    distances[root] = 0
    queue = [(0, root)]
    while queue:
        distance, node = heapq.heappop(queue)
        if distance > distances[node]:
            continue
        for neighbor, length in adjacency[node]:
            candidate = distance + length
            if candidate < distances[neighbor]:
                distances[neighbor] = candidate
                heapq.heappush(queue, (candidate, neighbor))
    ordered = []
    for a, b, color in edges:
        if distances[a] > distances[b]:
            a, b = b, a
        ordered.append((a, b, color, distances[a], math.dist(vertices[a], vertices[b])))
    extent = max(start + length for _, _, _, start, length in ordered)
    return vertices, ordered, distances, extent, root


def project_arm(vertices, center, scale, progress, rotation=0.0, pitch=0.24):
    yaw = 0.5 + progress * math.tau * 0.65 + rotation
    projected = []
    for x, y, z in vertices:
        # rotate around the base axis so the seed stays fixed as the arm turns
        x += 20
        rx = x * math.cos(yaw) + z * math.sin(yaw)
        rz = -x * math.sin(yaw) + z * math.cos(yaw)
        ry = y * math.cos(pitch) - rz * math.sin(pitch)
        depth = y * math.sin(pitch) + rz * math.cos(pitch)
        perspective = 560 / (560 + depth)
        projected.append((center[0] + rx * scale * perspective,
                          center[1] - ry * scale * perspective, depth))
    return projected


def point_opacity(elapsed, born, finished):
    fade_start = max(born + 1.2, finished)
    if elapsed < born:
        return 0.0
    return max(0.0, min(1.0, 1 - (elapsed - fade_start) / 0.15))


def arm_line_color(accent, secondary=False):
    if secondary == "terrain":
        return ImageColor.getrgb(TERRAIN)
    return ImageColor.getrgb(SECONDARY if secondary else accent)


def point_coverage(layer):
    # fade white points by pixel coverage so underlying lines keep their hue
    bounds = layer.getchannel("A").getbbox()
    if bounds is None:
        return layer
    thresholds = ((0, 7, 3), (6, 5, 2), (4, 1, 8))
    pixels = layer.load()
    for y in range(bounds[1], bounds[3]):
        for x in range(bounds[0], bounds[2]):
            red, green, blue, alpha = pixels[x, y]
            if 0 < alpha < 255:
                visible = alpha >= (thresholds[y % 3][x % 3] + 0.5) * 255 / 9
                pixels[x, y] = (red, green, blue, 255 if visible else 0)
    return layer


def robot_arm(draw, center, scale, progress, accent, build=1.0, show_points=True, rotation=0.0, pitch=0.24,
              elapsed_seconds=None, drawing_seconds=4.5, point_draw=None):
    """Rotate the wireframe while a connected drawing front grows from the base."""
    vertices, edges, distances, extent, _ = arm_drawing_graph()
    frontier = max(0, min(1, build)) * extent
    projected = project_arm(vertices, center, scale, progress, rotation, pitch)
    # each edge starts only after the drawing front reaches its first endpoint
    tips = []
    for a, b, secondary, start, length in sorted(
            edges, key=lambda edge: -(projected[edge[0]][2] + projected[edge[1]][2])):
        if frontier <= start or length == 0:
            continue
        fraction = min(1, (frontier - start) / length)
        # advance in model space before projection to keep tips attached during rotation
        if fraction < 1:
            tip = tuple(vertices[a][i] + (vertices[b][i] - vertices[a][i]) * fraction
                        for i in range(3))
            tip_projected = project_arm([tip], center, scale, progress, rotation, pitch)[0]
        else:
            tip_projected = projected[b]
        end = tip_projected[:2]
        color = arm_line_color(accent, secondary)
        draw.line((projected[a][:2], end), fill=color, width=max(1, round(scale * 1.5)))
        if fraction < 1:
            tips.append(end)
    if show_points and scale >= 0.5:
        # use real time so branches and unequal edge lengths have consistent lifetimes
        elapsed = build * drawing_seconds if elapsed_seconds is None else elapsed_seconds
        births = [distance / extent * drawing_seconds for distance in distances]
        finishes = list(births)
        for a, _, _, start, length in edges:
            finishes[a] = max(finishes[a], (start + length) / extent * drawing_seconds)
        point_draw = point_draw if point_draw is not None else draw
        rgb = ImageColor.getrgb(INK)
        for index, born in enumerate(births):
            opacity = point_opacity(elapsed, born, finishes[index])
            if opacity <= 0:
                continue
            x, y, _ = projected[index]
            point_draw.rectangle((x - 1, y - 1, x + 1, y + 1), fill=rgb + (round(opacity * 255),))
        for x, y in tips:
            point_draw.point((round(x), round(y)), fill=rgb + (255,))


def profile_name_size(name):
    size = 50
    measure = ImageDraw.Draw(Image.new("L", (1, 1)))
    while size > 24 and measure.textlength(name, font=font(size)) > 720:
        size -= 1
    return size


def language_colors(config):
    languages = sorted(((name, count) for name, count in config.get("languages", {}).items() if count),
                       key=lambda item: (-item[1], item[0]))
    palette = (config["accent"], SECONDARY, "#769fe0", "#ffbb8a")
    return {**{name: color
               for (name, _), color in zip(languages[:4], palette)}, "Other": "#665673"}


def language_chart(draw, config):
    languages = sorted(((name, count) for name, count in config.get("languages", {}).items() if count),
                       key=lambda item: (-item[1], item[0]))
    total = sum(count for _, count in languages)
    if not total:
        text(draw, (42, 263), config["footer"], 12, 720, 1, MUTED)
        return
    entries = languages[:4]
    other = sum(count for _, count in languages[4:])
    if other:
        entries.append(("Other", other))
    colors = language_colors(config)
    text(draw, (42, 219), "LANGUAGES / PUBLIC CODE", 11, 720, 1, MUTED)
    cumulative = 0
    legend_x = 42
    for name, count in entries:
        color = colors[name]
        left = 42 + round(720 * cumulative / total)
        cumulative += count
        right = 42 + round(720 * cumulative / total)
        if right > left:
            draw.rectangle((left, 241, right - 1, 250), fill=color)
        label = f"{name} {100 * count / total:.1f}%"
        bounds = draw.textbbox((legend_x + 13, 263), label, font=font(11))
        square_top = (bounds[1] + bounds[3] - 1) // 2 - 3
        draw.rectangle((legend_x, square_top, legend_x + 6, square_top + 6), fill=color)
        text(draw, (legend_x + 13, 263), label, 11, 720 - (legend_x - 42) - 11, 1, MUTED)
        legend_x += round(draw.textlength(label, font=font(11))) + 31


def recent_repositories(months):
    repositories = {}
    for month in months:
        for repo in month.get("items", []):
            key = repo["name"].casefold()
            if key not in repositories:
                repositories[key] = {**repo, "commits": 0}
            repositories[key]["commits"] += repo.get("commits", 0)
            if repo["last_commit"] > repositories[key]["last_commit"]:
                repositories[key]["last_commit"] = repo["last_commit"]
                repositories[key]["name"] = repo["name"]
    return sorted(repositories.values(), key=lambda repo: (-date.fromisoformat(repo["last_commit"]).toordinal(),
                                                          repo["name"].casefold()))


def activity_charts(draw, config):
    colors = language_colors(config)
    text(draw, (42, 292), "RECENTLY ACTIVE REPOSITORIES", 11, 530, 1, MUTED)
    text(draw, (620, 293), "COMMITS / 6 MONTHS", 9, 142, 1, MUTED)
    for index, repo in enumerate(recent_repositories(config.get("monthly_repositories", []))[:4]):
        x, y = 42 + index * 183, 314
        owner, _, name = repo["name"].partition("/")
        label = name if owner.casefold() == config.get("github_username", "").casefold() else repo["name"]
        while draw.textlength(label, font=font(12)) > 147:
            label = label[:-4] + "..."
        draw.rounded_rectangle((x, y, x + 171, y + 70), radius=5, fill=PANEL, outline="#49305e")
        draw.line((x + 12, y + 27, x + 159, y + 27), fill="#49305e")
        text(draw, (x + 12, y + 9), label, 12, 147, 1, INK)
        language = config.get("repository_details", {}).get(repo["name"].casefold(), {}).get("language") or "Code"
        language_color = colors.get(language, colors["Other"])
        bounds = draw.textbbox((x + 24, y + 34), language, font=font(10))
        center_y = (bounds[1] + bounds[3]) // 2
        center_x = x + 15
        draw.polygon([(center_x, center_y - 3), (center_x + 3, center_y),
                      (center_x, center_y + 3), (center_x - 3, center_y)], fill=language_color)
        text(draw, (x + 24, y + 34), language, 10, 78, 1, MUTED)
        count = repo["commits"]
        count_label = f"{count} {'commit' if count == 1 else 'commits'}"
        count_x = x + 159 - round(draw.textlength(count_label, font=font(10)))
        text(draw, (count_x, y + 34), count_label, 10, 72, 1, SECONDARY)
        stamp = date.fromisoformat(repo["last_commit"]).strftime("%b %d")
        text(draw, (x + 12, y + 55), "UPDATED", 9, 65, 1, MUTED)
        stamp_x = x + 159 - round(draw.textlength(stamp, font=font(9)))
        text(draw, (stamp_x, y + 55), stamp, 9, 65, 1, MUTED)

    text(draw, (808, 78), "WEEKLY COMMITS", 15, 250, 1, INK)
    weeks = config.get("weekly_activity", [])
    if not weeks:
        text(draw, (808, 139), "Refresh stats to load", 10, 250, 1, MUTED)
        return
    values = [week["commits"] for week in weeks]
    text(draw, (808, 110), f"{len(weeks)} WEEKS / {sum(values):,} COMMITS", 11, 250, 1, SECONDARY)
    ceiling = max(10, math.ceil(max(values) / 10) * 10)
    left, right, top, bottom = 840, 1055, 156, 342
    for value in (0, ceiling // 2, ceiling):
        y = bottom - (bottom - top) * value / ceiling
        draw.line((left, y, right, y), fill="#49305e")
        text(draw, (800, round(y) - 6), str(value), 10, 35, 1, MUTED)
    points = [(left + i * (right - left) / max(1, len(values) - 1),
               bottom - (bottom - top) * count / ceiling) for i, count in enumerate(values)]
    if len(points) > 1:
        draw.polygon([(left, bottom), *points, (right, bottom)], fill="#281b3d")
        # grid lines stay visible through the filled area
        for value in (0, ceiling // 2, ceiling):
            y = bottom - (bottom - top) * value / ceiling
            draw.line((left, y, right, y), fill="#49305e")
        draw.line(points, fill=config["accent"], width=2)
    for x, y in points:
        draw.rectangle((x - 1, y - 1, x + 1, y + 1), fill=SECONDARY)
    for index in sorted({0, len(weeks) // 3, 2 * len(weeks) // 3, len(weeks) - 1}):
        day = date.fromisoformat(weeks[index]["start"])
        x = left + index * (right - left) / max(1, len(weeks) - 1)
        draw.line((x, bottom, x, bottom + 4), fill=MUTED)
        text(draw, (round(x) - 12, 356), day.strftime("%b"), 10, 35, 1, MUTED)
    start = date.fromisoformat(weeks[0]["start"])
    end = date.fromisoformat(weeks[-1]["start"]) + timedelta(days=6)
    text(draw, (808, 379), f"{start:%b %d} - {end:%b %d} UTC", 10, 250, 1, MUTED)


def profile_scene(config, ready=True):
    image = backdrop(config["accent"])
    draw = ImageDraw.Draw(image)
    accent = config["accent"]
    heading = config["eyebrow"] + (" [ READY ]" if ready else "")
    text(draw, (72, 33), heading, 12, 780, 1, accent)
    status_x = 1028 - round(draw.textlength("dylan@couchpugtato:~$", font=font(11)))
    text(draw, (status_x, 33), "dylan@couchpugtato:~$", 11, 250, 1, SECONDARY)
    text(draw, (40, 64), config["name"], profile_name_size(config["name"]), 720, 1)
    draw.line((42, 124, 115, 124), fill=SECONDARY, width=2)
    text(draw, (42, 145), config["description"], 16, 720, 3, MUTED)
    stats = config.get("stats", [])
    if stats:
        labels = {"PUBLIC REPOSITORIES": "public repos", "REPOSITORY STARS": "stars"}
        summary = "   /   ".join(
            f"{stat['value']} {labels.get(stat['label'], stat['label'].lower())}"
            for stat in stats)
        text(draw, (72, 400), summary, 12, 650, 1, "#a294af")
    activity_charts(draw, config)
    language_chart(draw, config)
    draw.line((780, 72, 780, 384), fill="#49305e")
    draw.line((42, 390, 1058, 390), fill="#49305e")
    if config.get("linkedin_url"):
        label = config["linkedin_url"].removeprefix("https://").removeprefix("www.").rstrip("/")
        link_x = 1028 - round(draw.textlength(label, font=font(12)))
        text(draw, (link_x, 400), label, 12, 300, 1, SECONDARY)
    return image


def initialization_progress(draw, config, percent):
    text(draw, (447, 142), "INITIALIZING", 38, 590, 1, INK)
    text(draw, (450, 201), f"{percent:03d}% / profile.service", 16, 590, 1, config["accent"])
    slots = 54
    filled = min(slots, max(0, int(percent * slots / 100)))
    bar = "=" * slots if percent >= 100 else "=" * filled + ">" + " " * (slots - filled - 1)
    draw.text((450, 235), f"[{bar}]", font=font(16), fill=config["accent"])


def loading_scene(config, progress):
    image = backdrop(config["accent"], show_sun=False, emphasis=1, show_terrain=False)
    draw = ImageDraw.Draw(image)
    accent = config["accent"]
    text(draw, (72, 33), "LINUX / PROFILE INITIALIZATION", 12, 780, 1, accent)
    status_x = 1028 - round(draw.textlength("systemd[1]", font=font(11)))
    text(draw, (status_x, 33), "systemd[1]", 11, 110, 1, SECONDARY)
    points_layer = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    robot_arm(draw, (255, 200), 1.1, progress, accent,
              build=min(1, progress / 0.9), rotation=progress * math.pi / 4,
              elapsed_seconds=progress * config["duration_seconds"] * 5 / TIMELINE_SECONDS,
              drawing_seconds=config["duration_seconds"] * 4.5 / TIMELINE_SECONDS,
              point_draw=ImageDraw.Draw(points_layer))
    image = Image.alpha_composite(image.convert("RGBA"), point_coverage(points_layer)).convert("RGB")
    draw = ImageDraw.Draw(image)
    text(draw, (450, 111), "[ init ] launching profile services", 12, 570, 1, SECONDARY)
    initialization_progress(draw, config, round(progress * 100))
    if progress == 0:
        stage = "[ .... ] Initializing renderer.service"
    elif progress < 0.2:
        stage = "[ .... ] Constructing base geometry"
    elif progress < 0.6:
        stage = "[ .... ] Tracing joints and links"
    elif progress < 0.9:
        stage = "[ .... ] Connecting wrist and gripper"
    else:
        stage = "[ OK ] Started robot-arm.service"
    text(draw, (450, 273), stage, 13, 590, 1, MUTED)
    status = ("[ .... ] profile.service: waiting for renderer" if progress < 0.9
              else "[ .... ] Starting profile.service")
    text(draw, (72, 400), status, 11, 956, 1, accent)
    return image


@lru_cache(maxsize=1)
def reconstruction_particles():
    """Route blue beam samples into terrain and other samples into the sun."""
    sun_targets = [(x, y) for y, left, right, _ in sun_bands()
                   for x in range(left, right + 1)]
    mask = terrain_wire_mask().load()
    terrain_targets = [(x, y) for y in range(242, 287) for x in range(SIZE[0]) if mask[x, y]]
    vertices, edges = arm_mesh()
    points = project_arm(vertices, (255, 200), 1.1, 1.0, rotation=math.pi / 4)
    pools = {"sun": [edge for edge in edges if edge[2] != "terrain"],
             "terrain": [edge for edge in edges if edge[2] == "terrain"]}
    rng = random.Random(509)
    particles = []
    left = min(x for x, _ in sun_targets)
    right = max(x for x, _ in sun_targets)
    for destination, targets in (("sun", sun_targets), ("terrain", terrain_targets)):
        pool = pools[destination]
        for target in targets:
            a, b, material = pool[rng.randrange(len(pool))]
            t = rng.random()
            source = tuple(points[a][axis] * (1 - t) + points[b][axis] * t for axis in range(2))
            delay = (terrain_particle_delay(target[0]) if destination == "terrain"
                     else 0.45 * (right - target[0]) / max(1, right - left))
            particles.append((source, target, destination, delay, material))
    return particles


def particle_motion(destination, elapsed, delay):
    start = 0.52 if destination == "terrain" else 3.04
    return max(0, min(1, (elapsed - start - 2 * delay) / 1.2))


def formation_fades(elapsed):
    terrain = max(0, min(1, (elapsed - 2.64) / 0.4))
    sun = max(0, min(1, (elapsed - 5.14) / 0.4))
    return tuple(value * value * (3 - 2 * value) for value in (terrain, sun))


def reconstruction_scene(config, progress, final):
    elapsed = progress * 6.24
    terrain_settle, sun_settle = formation_fades(elapsed)
    backdrop_image = backdrop(config["accent"], show_sun=False, emphasis=1, show_terrain=False)
    loader = loading_scene(config, 1.0)
    image = Image.blend(loader, backdrop_image, min(1, elapsed / 0.46))
    image = Image.blend(image, backdrop(config["accent"], show_sun=False, emphasis=1), terrain_settle)
    image = Image.blend(image, backdrop(config["accent"], emphasis=1), sun_settle)
    glow = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow)
    settled_points = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    settled_draw = ImageDraw.Draw(settled_points)
    appearance = min(1, elapsed / 0.16)
    cores = []
    for source, target, destination, delay, material in reconstruction_particles():
        local = particle_motion(destination, elapsed, delay)
        ease = local * local * (3 - 2 * local)
        x = source[0] + (target[0] - source[0]) * ease
        y = source[1] + (target[1] - source[1]) * ease
        if destination == "terrain":
            arc_height = min(110, 35 + abs(target[0] - source[0]) * 0.12)
            y -= arc_height * math.sin(math.pi * ease)
        source_rgb = arm_line_color(config["accent"], material)
        target_rgb = ImageColor.getrgb(TERRAIN if destination == "terrain" else SECONDARY)
        rgb = tuple(round(source + (target - source) * ease) for source, target in zip(source_rgb, target_rgb))
        dissolve = terrain_settle if destination == "terrain" else sun_settle
        opacity = round(255 * appearance * (1 - dissolve))
        if not opacity:
            continue
        # halos fade as dense cores settle into their destination outlines
        halo_opacity = round(opacity * 0.18 * (1 - ease))
        if halo_opacity:
            draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=rgb + (halo_opacity,))
        cores.append((round(x), round(y), rgb, opacity, local >= 1))
    for x, y, rgb, opacity, arrived in cores:
        target_draw = settled_draw if arrived else draw
        target_draw.rectangle((x - 1, y - 1, x + 1, y + 1), fill=rgb + (opacity,))
    settled_points.putalpha(ImageChops.multiply(settled_points.getchannel("A"), reconstruction_mask()))
    glow = Image.alpha_composite(glow, settled_points)
    image = Image.alpha_composite(image.convert("RGBA"), glow).convert("RGB")
    footer_box = (65, 395, 1030, 420)
    image.paste(backdrop_image.crop(footer_box), footer_box[:2])
    stage = "terrain" if elapsed < 3.04 else "sun"
    status = f"[ .... ] profile.service: reconstructing {stage}"
    text(ImageDraw.Draw(image), (72, 400), status, 11, 956, 1, config["accent"])
    if elapsed > 5.94:
        fade = min(1, (elapsed - 5.94) / 0.3)
        fade = fade * fade * (3 - 2 * fade)
        image = Image.blend(image, profile_scene(config, ready=False), fade)
    return image


def scene(config, phase, final=None):
    elapsed = phase * TIMELINE_SECONDS
    if elapsed <= 1:
        return loading_scene(config, 0)
    if elapsed <= 6:
        return loading_scene(config, (elapsed - 1) / 5)
    if elapsed <= 6.15:
        return loading_scene(config, 1.0)
    final = final if final is not None else profile_scene(config)
    if elapsed >= 12.39:
        return final.copy()
    return reconstruction_scene(config, (elapsed - 6.15) / 6.24, final)


def render(config_path: Path, output: Path) -> tuple[Path, Path]:
    config = read_config(config_path)
    count = max(2, round(config["duration_seconds"] * config["fps"]))
    final = profile_scene(config)
    # include transition colors so the shared palette preserves the deeper violet
    palette_source = Image.new("RGB", (SIZE[0], SIZE[1] * 2))
    palette_source.paste(final, (0, 0))
    palette_source.paste(backdrop(config["accent"], emphasis=1), (0, SIZE[1]))
    palette = palette_source.quantize(colors=128)
    # reserve the exact purple orange and blue arm colors
    entries = palette.getpalette()
    arm_colors = []
    for secondary in (False, True, "terrain"):
        arm_colors.extend(arm_line_color(config["accent"], secondary))
    entries[128 * 3:131 * 3] = arm_colors
    palette.putpalette(entries)
    frames = []
    for index in range(count):
        progress = index / (count - 1)
        frame = scene(config, progress, final)
        frames.append(frame.quantize(palette=palette, dither=Image.Dither.NONE))
    output.parent.mkdir(parents=True, exist_ok=True)
    preview = output.with_suffix(".png")
    final.save(preview)
    # gif durations use 10ms ticks distribute rounding to preserve total duration
    ticks = round(config["duration_seconds"] * 100)
    durations = [10 * (round((i + 1) * ticks / count) - round(i * ticks / count)) for i in range(count)]
    # omit loop entirely: loop=1 may play twice disposal 1 retains the final frame
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:],
                   duration=durations, disposal=1, optimize=False)
    return output, preview


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "profile/content.json")
    parser.add_argument("--output", type=Path, default=ROOT / "assets/profile.gif")
    parser.add_argument("--refresh-stats", action="store_true",
                        help="Fetch live public GitHub statistics and save them in the config")
    args = parser.parse_args()
    if args.output.suffix.lower() != ".gif":
        parser.error("--output must end in .gif")
    try:
        if args.refresh_stats:
            refresh_github_stats(args.config.resolve())
        gif, preview = render(args.config.resolve(), args.output.resolve())
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(1, f"Cannot render profile: {error}\n")
    print(f"GIF: {gif} ({gif.stat().st_size / 1024:.0f} KiB)")
    print(f"Still preview: {preview}")


if __name__ == "__main__":
    main()
