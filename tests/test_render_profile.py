import json
from datetime import date
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageChops, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from render_profile import ROOT, SIZE, loading_scene, read_config, render, robot_arm, refresh_github_stats, github_weekly_activity, recent_repositories, profile_scene, scene, arm_drawing_graph, project_arm, point_opacity


class RendererTests(unittest.TestCase):
    def test_arm_links_keep_their_exact_colors_at_every_depth(self):
        for secondary, base in ((False, (187, 140, 255)), (True, (255, 154, 82))):
            graph = ([(0, 0, 0), (0, 10, 0)], [(0, 1, secondary, 0, 10)], [0, 10], 10, 0)
            for depth in (-100, 0, 80, 200):
                draw = Mock()
                with patch("render_profile.arm_drawing_graph", return_value=graph), \
                        patch("render_profile.project_arm", return_value=[(10, 10, depth), (20, 20, depth)]):
                    robot_arm(draw, (0, 0), 1, 1, "#bb8cff", show_points=False)
                color = draw.line.call_args.kwargs["fill"]
                self.assertEqual(color, base)

    def test_fading_white_points_do_not_desaturate_underlying_lines(self):
        from render_profile import point_coverage
        purple = (187, 140, 255, 255)
        white = (245, 237, 255, 255)
        points = Image.new("RGBA", (3, 3), (*white[:3], 128))
        faded = point_coverage(points)
        result = Image.alpha_composite(Image.new("RGBA", (3, 3), purple), faded)
        colors = {color: count for count, color in result.getcolors()}
        self.assertEqual(colors, {white: 5, purple: 4})

    def test_point_minimum_lifetime_and_smooth_fade(self):
        self.assertEqual(point_opacity(0.9, born=1, finished=1), 0)
        self.assertEqual(point_opacity(2.1, born=1, finished=1), 1)
        self.assertEqual(point_opacity(2.2, born=1, finished=1), 1)
        self.assertAlmostEqual(point_opacity(2.275, born=1, finished=1), 0.5)
        self.assertAlmostEqual(point_opacity(2.35, born=1, finished=1), 0)

    def test_point_remains_lit_until_outgoing_connections_finish(self):
        self.assertEqual(point_opacity(1.9, born=0, finished=2), 1)
        self.assertEqual(point_opacity(2, born=0, finished=2), 1)
        self.assertAlmostEqual(point_opacity(2.075, born=0, finished=2), 0.5)
        self.assertAlmostEqual(point_opacity(2.15, born=0, finished=2), 0)

    def test_parent_fades_while_new_rear_vertex_stays_visible(self):
        graph = ([(0, 0, 0), (0, 10, 0)], [(0, 1, False, 0, 10)], [0, 10], 10, 0)
        points = [(10, 10, -100), (20, 20, 200)]
        with patch("render_profile.arm_drawing_graph", return_value=graph), \
                patch("render_profile.project_arm", return_value=points):
            dots = Mock()
            robot_arm(Mock(), (0, 0), 1, 1, "#bb8cff", build=1,
                      elapsed_seconds=2.075, drawing_seconds=2, point_draw=dots)
        calls = dots.rectangle.call_args_list
        self.assertEqual([call.args[0] for call in calls], [(9, 9, 11, 11), (19, 19, 21, 21)])
        self.assertIn(calls[0].kwargs["fill"][3], (127, 128))
        self.assertEqual(calls[1].kwargs["fill"][3], 255)

    def test_arm_seed_is_stationary_throughout_rotation(self):
        import math
        vertices, _, _, _, root = arm_drawing_graph()
        anchor = project_arm([vertices[root]], (255, 200), 1.1, 0)[0]
        for step in range(61):
            progress = step / 60
            point = project_arm([vertices[root]], (255, 200), 1.1, progress,
                                rotation=progress * math.pi / 4)[0]
            for actual, expected in zip(point, anchor):
                self.assertAlmostEqual(actual, expected, places=10)

    def test_growing_tip_is_projected_from_its_3d_position(self):
        import math
        vertices = [(-20, 0, -70), (20, 80, 70)]
        length = math.dist(*vertices)
        graph = (vertices, [(0, 1, False, 0, length)], [0, length], length, 0)
        midpoint = tuple((a + b) / 2 for a, b in zip(*vertices))
        for progress in (0, 0.25, 0.5, 0.75, 1):
            draw = Mock()
            with patch("render_profile.arm_drawing_graph", return_value=graph):
                robot_arm(draw, (200, 200), 1, progress, "#bb8cff", build=0.5)
            endpoint = draw.line.call_args.args[0][1]
            expected = project_arm([midpoint], (200, 200), 1, progress)[0][:2]
            for actual, target in zip(endpoint, expected):
                self.assertAlmostEqual(actual, target, places=10)

    def test_ready_indicator_waits_for_complete_profile(self):
        config = read_config(ROOT / "profile/content.json")
        ready = profile_scene(config)
        waiting = profile_scene(config, ready=False)
        indicator = ImageChops.difference(ready, waiting).getbbox()
        self.assertIsNotNone(indicator)
        with patch("render_profile.profile_scene", wraps=profile_scene) as draw_profile:
            scene(config, 0.979, final=ready)
            draw_profile.assert_called_once_with(config, ready=False)
        self.assertIsNone(ImageChops.difference(scene(config, 0.989).crop(indicator),
                                               ready.crop(indicator)).getbbox())

    def test_final_arm_has_no_vertex_highlights(self):
        image = Image.new("RGB", (400, 400))
        robot_arm(ImageDraw.Draw(image), (200, 200), 1, 0.14, "#bb8cff", show_points=False)
        self.assertIsNotNone(image.getbbox())
        self.assertNotIn((245, 237, 255),
                         [color for _, color in image.getcolors(image.width * image.height)])

    def test_single_play_timing_and_final_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            gif, png = render(ROOT / "profile/content.json", Path(directory) / "profile.gif")
            with Image.open(gif) as animation, Image.open(png) as preview:
                self.assertNotIn("loop", animation.info)
                self.assertGreater(animation.n_frames, 1)
                self.assertEqual(animation.size, SIZE)
                config = read_config(ROOT / "profile/content.json")
                self.assertGreaterEqual(animation.info["duration"],
                                        round(config["duration_seconds"] * 100),
                                        "Opening frame must pause before drawing starts")
                palette = animation.copy()
                expected_opening = loading_scene(config, 0).quantize(
                    palette=palette, dither=Image.Dither.NONE).convert("RGB")
                self.assertIsNone(ImageChops.difference(
                    animation.convert("RGB"), expected_opening).getbbox(),
                    "Opening frame must be the full-screen loader, not the profile")
                duration = 0
                for index in range(animation.n_frames):
                    animation.seek(index)
                    duration += animation.info["duration"]
                    self.assertEqual(animation.disposal_method, 1)
                self.assertEqual(duration, round(config["duration_seconds"] * 100) * 10)
                self.assertGreaterEqual(animation.info["duration"],
                                        round(1000 / config["fps"]))
                rgb = preview.convert("RGB")
                expected = rgb.quantize(palette=palette,
                                        dither=Image.Dither.NONE).convert("RGB")
                difference = ImageChops.difference(animation.convert("RGB"), expected)
                self.assertIsNone(difference.getbbox(), "GIF must end on the complete preview")
            self.assertNotIn(b"NETSCAPE2.0", gif.read_bytes())

    def test_custom_profile_config_and_output(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = json.loads((ROOT / "profile/content.json").read_text(encoding="utf-8"))
            config.update(duration_seconds=1, fps=5, name="Example Person",
                          description="Computer Science student and robotics research assistant at Example University.")
            path = base / "content.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            gif, preview = render(path, base / "nested/profile.gif")
            self.assertTrue(preview.is_file())
            with Image.open(gif) as image:
                self.assertEqual(image.size, SIZE)
                self.assertGreater(image.n_frames, 1)
            del config["description"]
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "description"):
                read_config(path)

    def test_invalid_content_fails_with_useful_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "content.json"
            config = json.loads((ROOT / "profile/content.json").read_text())
            config["fps"] = 0
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "fps"):
                read_config(path)
            config["fps"] = 15
            config["description"] = "long " * 100
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "lines"):
                render(path, Path(directory) / "profile.gif")


class GitHubStatsTests(unittest.TestCase):
    def test_pagination_and_public_star_total(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "content.json"
            path.write_text(json.dumps({"github_username": "example"}), encoding="utf-8")
            responses = [
                {"followers": 7, "public_repos": 101},
                [{"stargazers_count": 2, "fork": True}] * 100,
                [{"stargazers_count": 5, "full_name": "example/code"},
                 {"stargazers_count": 99, "private": True}],
                {"Python": 1000},
                {"total_count": 1234, "incomplete_results": False},
            ]
            with patch("render_profile.github_weekly_activity", return_value=([], [])), \
                    patch("render_profile.github_api", side_effect=responses) as api:
                stats = refresh_github_stats(path)
            self.assertEqual([stat["value"] for stat in stats], ["1,234", "7", "101", "205"])
            self.assertEqual(stats[0]["label"], "COMMITS")
            self.assertIn("page=2", api.call_args_list[2].args[0])
            self.assertEqual(json.loads(path.read_text())["stats"], stats)

    def test_language_totals_exclude_forks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "content.json"
            path.write_text(json.dumps({"github_username": "example"}), encoding="utf-8")
            responses = {
                "/users/example": {"followers": 2, "public_repos": 3},
                "/users/example/repos?type=owner&per_page=100&page=1": [
                    {"full_name": "example/one", "stargazers_count": 1},
                    {"full_name": "example/two", "stargazers_count": 2},
                    {"full_name": "example/fork", "stargazers_count": 3, "fork": True},
                ],
                "/repos/example/one/languages": {"Python": 100, "Rust": 200,
                                                 "JavaScript": 20, "CSS": 30, "HTML": 40},
                "/repos/example/two/languages": {"Python": 300, "C": 400, "CSS": 50},
                "/search/commits?q=author%3Aexample+is%3Apublic&per_page=1":
                    {"total_count": 321, "incomplete_results": False},
            }
            with patch("render_profile.github_weekly_activity", return_value=([], [])), \
                    patch("render_profile.github_api", side_effect=lambda url: responses[url]) as api:
                refresh_github_stats(path)
            self.assertEqual(json.loads(path.read_text())["languages"],
                             {"C": 400, "Python": 400, "Rust": 200, "JavaScript": 140})
            self.assertEqual(json.loads(path.read_text())["language_repos"],
                             {"Python": 2, "JavaScript": 2, "C": 1, "Rust": 1})
            self.assertNotIn("/repos/example/fork/languages", [call.args[0] for call in api.call_args_list])

    def test_weekly_activity_uses_complete_monday_sunday_weeks(self):
        result = {"total_count": 2, "incomplete_results": False, "items": [
            {"commit": {"author": {"date": "2026-04-20T00:00:00Z"}}},
            {"commit": {"author": {"date": "2026-10-18T23:59:59Z"}}},
        ]}
        with patch("render_profile.github_api", return_value=result) as api:
            weeks = github_weekly_activity("example", today=date(2026, 10, 20))
        self.assertEqual(len(weeks), 26)
        self.assertEqual(weeks[0], {"start": "2026-04-20", "commits": 1})
        self.assertEqual(weeks[-1], {"start": "2026-10-12", "commits": 1})
        self.assertIn("2026-04-20..2026-10-18", api.call_args.args[0])
        self.assertEqual(api.call_count, 1)

    def test_weekly_activity_reads_every_result_page(self):
        item = {"commit": {"author": {"date": "2026-10-12T12:00:00Z"}}}
        responses = [
            {"total_count": 101, "incomplete_results": False, "items": [item] * 100},
            {"total_count": 101, "incomplete_results": False, "items": [item]},
        ]
        with patch("render_profile.github_api", side_effect=responses) as api:
            weeks = github_weekly_activity("example", today=date(2026, 10, 20))
        self.assertEqual(weeks[-1]["commits"], 101)
        self.assertEqual(sum(week["commits"] for week in weeks), 101)
        self.assertIn("page=2", api.call_args.args[0])

    def test_monthly_repositories_are_unique_and_include_current_month(self):
        entries = [("2026-08-01", "example/One"), ("2026-08-02", "example/one"),
                   ("2026-08-03", "example/two"), ("2026-10-20", "other/research")]
        result = {"total_count": 4, "incomplete_results": False, "items": [
            {"commit": {"author": {"date": day + "T12:00:00Z"}},
             "repository": {"full_name": repo}} for day, repo in entries]}
        with patch("render_profile.github_api", return_value=result) as api:
            weeks, months = github_weekly_activity("example", today=date(2026, 10, 20),
                                                  include_monthly=True)
        self.assertEqual(len(months), 6)
        self.assertEqual(months[0], {"month": "2026-05", "repositories": 0, "partial": False, "items": []})
        self.assertEqual(months[3]["repositories"], 2)
        self.assertEqual({repo["name"].casefold() for repo in months[3]["items"]},
                         {"example/one", "example/two"})
        one = next(repo for repo in months[3]["items"] if repo["name"].casefold() == "example/one")
        self.assertEqual(one["last_commit"], "2026-08-02")
        self.assertEqual(one["commits"], 2)
        self.assertTrue(months[-1]["partial"])
        self.assertEqual(months[-1]["items"][0]["name"], "other/research")
        self.assertEqual(sum(week["commits"] for week in weeks), 3)
        self.assertIn("..2026-10-20", api.call_args.args[0])

    def test_recent_repository_list_deduplicates_and_sorts_by_latest_commit(self):
        months = [{"items": [{"name": "example/One", "last_commit": "2026-08-01", "commits": 4}]},
                  {"items": [{"name": "example/one", "last_commit": "2026-10-20", "commits": 7},
                             {"name": "example/two", "last_commit": "2026-10-18", "commits": 3}]}]
        repos = recent_repositories(months)
        self.assertEqual([repo["name"] for repo in repos], ["example/one", "example/two"])
        self.assertEqual([repo["commits"] for repo in repos], [11, 3])
        self.assertEqual(months[0]["items"][0]["commits"], 4)

    def test_incomplete_activity_is_not_accepted(self):
        with patch("render_profile.github_api", return_value={"incomplete_results": True}):
            with self.assertRaisesRegex(ValueError, "incomplete weekly activity"):
                github_weekly_activity("example", today=date(2026, 10, 20))

    def test_api_failure_preserves_saved_stats(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "content.json"
            original = json.dumps({"github_username": "example", "stats": []})
            path.write_text(original, encoding="utf-8")
            with patch("render_profile.github_api", side_effect=[
                    {"followers": 7, "public_repos": 101}, OSError("Network unavailable")]):
                with self.assertRaises(OSError):
                    refresh_github_stats(path)
            self.assertEqual(path.read_text(), original)


if __name__ == "__main__":
    unittest.main()
