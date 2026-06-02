from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from image_compression import compress_image_file
from image_tool_core import (
    collect_image_paths,
    resize_image_file,
    resize_target_format_from_source,
    resize_target_suffix,
)
from output_naming import (
    DEFAULT_NAMING_TEMPLATE,
    MAX_TEMPLATE_CHARS,
    OutputNaming,
    default_template_from_config,
    validate_output_template,
)
from output_safety import commit_temporary_output, temporary_output_path


class ImageToolCoreTests(unittest.TestCase):
    def test_resize_uses_safe_output_format_for_read_only_suffixes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = Path(temp_dir)
            source = work_dir / "sample.dds"
            output_dir = work_dir / "out"
            Image.new("RGB", (12, 8), "red").save(source, format="PNG")

            self.assertEqual(resize_target_format_from_source(source), "png")
            self.assertEqual(resize_target_suffix(source), ".png")

            result = resize_image_file(source, output_dir, 50)

            self.assertEqual(result.target.suffix, ".png")
            self.assertTrue(result.target.exists())
            with Image.open(result.target) as output:
                self.assertEqual(output.format, "PNG")
                self.assertEqual(output.size, (6, 4))

    def test_collect_image_paths_enforces_import_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = Path(temp_dir)
            for index in range(3):
                Image.new("RGB", (1, 1), "blue").save(work_dir / f"{index}.png")

            with self.assertRaises(ValueError):
                collect_image_paths([work_dir], include_subfolders=False, max_images=2)

    def test_failed_resize_removes_temporary_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = Path(temp_dir)
            source = work_dir / "sample.png"
            output_dir = work_dir / "out"
            Image.new("RGB", (12, 8), "red").save(source)

            def fail_after_partial_write(
                _image,
                target: Path,
                _target_format: str,
                _keep_metadata: bool = False,
            ) -> None:
                target.write_bytes(b"partial")
                raise RuntimeError("simulated save failure")

            with mock.patch("image_tool_core.save_regular_image", side_effect=fail_after_partial_write):
                with self.assertRaises(RuntimeError):
                    resize_image_file(source, output_dir, 50)

            self.assertEqual(list(output_dir.iterdir()), [])

    def test_commit_temporary_output_avoids_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = Path(temp_dir)
            target = work_dir / "result.png"
            target.write_bytes(b"existing")
            temp_target = temporary_output_path(target)
            temp_target.write_bytes(b"new")

            final_path = commit_temporary_output(temp_target, target)

            self.assertEqual(final_path.name, "result_2.png")
            self.assertEqual(target.read_bytes(), b"existing")
            self.assertEqual(final_path.read_bytes(), b"new")
            self.assertFalse(temp_target.exists())

    def test_template_and_preserved_structure_control_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = Path(temp_dir)
            source_root = work_dir / "source"
            source_dir = source_root / "nested"
            source_dir.mkdir(parents=True)
            source = source_dir / "sample.png"
            output_dir = work_dir / "out"
            Image.new("RGB", (12, 8), "red").save(source)

            naming = OutputNaming(
                template="{name}_{width}x{height}_{operation}_{index}",
                preserve_structure=True,
                source_root=source_root,
                sequence=3,
            )
            result = resize_image_file(source, output_dir, 50, naming=naming)

            self.assertEqual(result.target.parent, output_dir / "nested")
            self.assertEqual(result.target.name, "sample_6x4_50pct_3.png")

    def test_validate_output_template_rejects_unknown_fields(self) -> None:
        with self.assertRaises(ValueError):
            validate_output_template("{name}_{unknown}")
        with self.assertRaises(ValueError):
            validate_output_template("{name.__class__}")
        with self.assertRaises(ValueError):
            validate_output_template("{date:>100000}")

    def test_default_template_from_config_uses_readable_timestamp_template(self) -> None:
        self.assertEqual(default_template_from_config(""), DEFAULT_NAMING_TEMPLATE)
        self.assertEqual(default_template_from_config("   "), DEFAULT_NAMING_TEMPLATE)
        self.assertEqual(
            default_template_from_config("x" * (MAX_TEMPLATE_CHARS + 1)),
            DEFAULT_NAMING_TEMPLATE,
        )
        self.assertEqual(default_template_from_config("{name.__class__}"), DEFAULT_NAMING_TEMPLATE)

    def test_validate_output_template_rejects_overlong_templates(self) -> None:
        with self.assertRaises(ValueError):
            validate_output_template("x" * (MAX_TEMPLATE_CHARS + 1))

    def test_target_size_compression_reports_reached_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = Path(temp_dir)
            source = work_dir / "photo.jpg"
            output_dir = work_dir / "out"
            Image.new("RGB", (640, 480), "white").save(source, quality=95)

            result = compress_image_file(source, output_dir, "中度", target_bytes=12 * 1024)

            self.assertTrue(result.target.exists())
            self.assertTrue(result.target_size_reached)
            self.assertLessEqual(result.output_bytes, 12 * 1024)

    def test_resize_strips_or_keeps_dpi_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = Path(temp_dir)
            source = work_dir / "dpi.jpg"
            Image.new("RGB", (20, 20), "white").save(source, dpi=(300, 300))

            stripped = resize_image_file(source, work_dir / "stripped", 50, keep_metadata=False)
            kept = resize_image_file(source, work_dir / "kept", 50, keep_metadata=True)

            with Image.open(stripped.target) as stripped_image:
                self.assertNotIn("dpi", stripped_image.info)
            with Image.open(kept.target) as kept_image:
                self.assertIn("dpi", kept_image.info)


class ImageToolGuiConfigTests(unittest.TestCase):
    def test_load_config_ignores_json_that_is_not_an_object(self) -> None:
        import image_tool_gui

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "settings.json"
            legacy_path = Path(temp_dir) / "legacy.json"
            config_path.write_text("[1, 2, 3]", encoding="utf-8")

            with mock.patch.object(image_tool_gui, "CONFIG_PATH", config_path), mock.patch.object(
                image_tool_gui,
                "LEGACY_CONFIG_PATH",
                legacy_path,
            ):
                self.assertEqual(image_tool_gui.load_config(), {})


if __name__ == "__main__":
    unittest.main()
