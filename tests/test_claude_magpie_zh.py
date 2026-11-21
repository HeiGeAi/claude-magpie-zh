import hashlib
import json
import os
import plistlib
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import claude_magpie_zh as cmz


def _make_raw_asar(files: dict[str, bytes]) -> bytes:
    header: dict = {"files": {}}
    chunks: list[bytes] = []
    offset = 0
    for rel, content in sorted(files.items()):
        parts = rel.split("/")
        node = header
        for part in parts[:-1]:
            node = node["files"].setdefault(part, {"files": {}})
        node["files"][parts[-1]] = {"size": len(content), "offset": str(offset)}
        chunks.append(content)
        offset += len(content)
    h_bytes = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    p_size = cmz.align4(4 + len(h_bytes))
    h_size = 4 + p_size
    prefix = struct.pack("<II", 4, h_size)
    padding = b"\x00" * (p_size - 4 - len(h_bytes))
    payload = struct.pack("<II", p_size, len(h_bytes)) + h_bytes + padding
    return prefix + payload + b"".join(chunks)


class AsarRoundTripTests(unittest.TestCase):
    def test_extract_build_and_verify_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            src.mkdir()
            (src / "a.txt").write_text("hello", encoding="utf-8")
            (src / "sub").mkdir()
            (src / "sub/b.json").write_text('{"k":"中文"}', encoding="utf-8")

            asar_path = root / "test.asar"
            cmz.build_asar(src, asar_path, unpacked=set())

            raw = asar_path.read_bytes()
            hs, _, hdr = cmz.read_asar_header(raw, asar_path)
            self.assertEqual(cmz.read_asar_entry(raw, hs, hdr, "a.txt"), b"hello")
            self.assertEqual(
                cmz.read_asar_entry(raw, hs, hdr, "sub/b.json"),
                '{"k":"中文"}'.encode("utf-8"),
            )

            extracted = root / "out"
            cmz.extract_asar(asar_path, extracted)
            self.assertEqual((extracted / "a.txt").read_text(encoding="utf-8"), "hello")
            self.assertEqual((extracted / "sub/b.json").read_text(encoding="utf-8"), '{"k":"中文"}')

    def test_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asar_path = root / "evil.asar"
            asar_path.write_bytes(_make_raw_asar({"../escape.txt": b"boom"}))
            with self.assertRaises(RuntimeError):
                cmz.extract_asar(asar_path, root / "dst")

    def test_rejects_short_header_and_out_of_bounds_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            short_hdr = root / "short.asar"
            short_hdr.write_bytes(struct.pack("<II", 4, 4) + b"\x00" * 16)
            with self.assertRaises(RuntimeError):
                cmz.read_asar_header(short_hdr.read_bytes(), short_hdr)

            truncated = root / "truncated.asar"
            raw = _make_raw_asar({"a.txt": b"hello world"})
            truncated.write_bytes(raw[:-3])
            data = truncated.read_bytes()
            hs, _, hdr = cmz.read_asar_header(data, truncated)
            with self.assertRaises(RuntimeError):
                cmz.read_asar_entry(data, hs, hdr, "a.txt")
            with self.assertRaises(RuntimeError):
                cmz.extract_asar(truncated, root / "out")


class PatchLogicTests(unittest.TestCase):
    def test_language_whitelist_and_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            js = Path(tmp) / "chunk.js"
            js.write_text(
                f'const L={cmz.BASE_LANG_LIST}];',
                encoding="utf-8",
            )
            hits1 = cmz.patch_language_whitelist([js], "zh-CN")
            self.assertEqual(hits1, 1)
            self.assertIn(',"zh-CN"]', js.read_text(encoding="utf-8"))

            hits2 = cmz.patch_language_whitelist([js], "zh-CN")
            self.assertEqual(hits2, 1)
            self.assertEqual(js.read_text(encoding="utf-8").count('"zh-CN"'), 1)

    def test_structural_context_is_protected(self) -> None:
        rules = cmz.compile_hardcoded_rules([
            ("Copy", "复制"),
            ("Settings", "设置"),
            ("New chat", "新对话"),
            ("Save changes", "保存更改"),
        ])
        js_code = (
            'const a={iconName:"Copy",label:"New chat"};'
            'const b={as:"Settings",title:"Save changes"};'
            'function F({iconName:t}){const x=t??"Copy";return x}'
        )
        updated, count = cmz.apply_rules_to_js_text(js_code, rules)
        self.assertIn('iconName:"Copy"', updated)
        self.assertIn('as:"Settings"', updated)
        self.assertIn('t??"Copy"', updated)
        self.assertIn('label:"新对话"', updated)
        self.assertIn('title:"保存更改"', updated)
        self.assertEqual(count, 2)

    def test_display_names_patch_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            idx = Path(tmp) / "index-123.js"
            idx.write_text("console.log(1);", encoding="utf-8")
            self.assertEqual(cmz.patch_display_names([idx]), 1)
            self.assertEqual(cmz.patch_display_names([idx]), 0)
            self.assertIn("简体中文", idx.read_text(encoding="utf-8"))

    def test_merge_i18n_catalog_with_english_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            en = root / "en-US.json"
            zh = root / "frontend-zh-CN.json"
            out = root / "zh-CN.json"
            en.write_text(json.dumps({"a": "Hello", "b": "Untranslated"}), encoding="utf-8")
            zh.write_text(json.dumps({"a": "你好"}), encoding="utf-8")
            translated, fallback = cmz.merge_i18n_catalog(en, zh, out)
            self.assertEqual((translated, fallback), (1, 1))
            merged = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(merged, {"a": "你好", "b": "Untranslated"})


class IntegrityAndConfigTests(unittest.TestCase):
    def test_electron_integrity_digest_and_sentinel_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "Claude.app"
            res_dir = app / "Contents/Resources"
            fw_dir = app / "Contents/Frameworks/Electron Framework.framework/Versions/A"
            res_dir.mkdir(parents=True)
            fw_dir.mkdir(parents=True)

            ion_src = Path(tmp) / "ion-src"
            (ion_src / "i18n/dynamic").mkdir(parents=True)
            (ion_src / "i18n/zh-CN.json").write_text(
                json.dumps({"hello": "你好世界"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (ion_src / "i18n/dynamic/zh-CN.json").write_text("{}", encoding="utf-8")
            ion_asar = res_dir / "ion-dist.asar"
            cmz.build_asar(ion_src, ion_asar, set())

            app_src = Path(tmp) / "app-src"
            app_src.mkdir()
            (app_src / "package.json").write_text('{"name":"claude"}', encoding="utf-8")
            app_asar = res_dir / "app.asar"
            cmz.build_asar(app_src, app_asar, set())

            _, app_h, _ = cmz.read_asar_header(app_asar.read_bytes(), app_asar)
            plist_data = {
                "CFBundleShortVersionString": "5.0.0",
                "CFBundleIdentifier": "com.example.claude",
                "ElectronAsarIntegrity": {
                    "Resources/app.asar": {
                        "algorithm": "SHA256",
                        "hash": hashlib.sha256(app_h.encode("utf-8")).hexdigest(),
                    },
                    "Resources/ion-dist.asar": {
                        "algorithm": "SHA256",
                        "hash": "0" * 64,
                    },
                },
            }
            with (app / "Contents/Info.plist").open("wb") as f:
                plistlib.dump(plist_data, f, fmt=plistlib.FMT_BINARY)

            fw_bin = fw_dir / "Electron Framework"
            fw_bin.write_bytes(b"\x00\x01" + cmz.INTEGRITY_SENTINEL + b"\x01\x00" + (b"\xaa" * 32) + b"\xff")

            slots = cmz.update_asar_integrity(app, "Resources/ion-dist.asar", ion_asar)
            self.assertEqual(slots, 1)

            info = cmz.verify_app(app, "zh-CN", check_codesign=False)
            self.assertEqual(info["cn_strings"], 1)
            self.assertEqual(info["version"], "5.0.0")

    def test_update_profile_locales(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            p1 = fake_home / "Library/Application Support/Claude-Magpie-3p"
            p2 = fake_home / "Library/Application Support/Claude"
            p1.mkdir(parents=True)
            p2.mkdir(parents=True)
            (p1 / "config.json").write_text('{"locale":"en-US","theme":"light"}', encoding="utf-8")

            updated = cmz.update_profile_locales("zh-CN", home=fake_home)
            self.assertEqual(len(updated), 2)
            self.assertEqual(json.loads((p1 / "config.json").read_text())["locale"], "zh-CN")
            self.assertEqual(json.loads((p1 / "config.json").read_text())["theme"], "light")
            self.assertEqual(json.loads((p2 / "config.json").read_text())["locale"], "zh-CN")

    def test_update_profile_locales_preserves_bom_and_skips_invalid_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            support = home / "Library/Application Support"
            config = support / "Claude" / "config.json"
            invalid = support / "Claude-Magpie-3p" / "config.json"
            config.parent.mkdir(parents=True)
            invalid.parent.mkdir()
            config.write_text('{"theme":"dark","locale":"de-DE"}', encoding="utf-8-sig")
            invalid.write_bytes(b'{"theme":"light",}')

            self.assertEqual(cmz.update_profile_locales("zh-CN", home=home), [config])
            self.assertEqual(json.loads(config.read_text()), {"theme": "dark", "locale": "zh-CN"})
            self.assertEqual(invalid.read_bytes(), b'{"theme":"light",}')

    def test_stage_refuses_source_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Claude.app"
            source.mkdir()
            marker = source / "preserve"
            marker.write_text("original")
            alias = root / "Alias.app"
            alias.symlink_to(source, target_is_directory=True)
            for destination in [source, alias, root, source / "nested" / "Claude.app"]:
                with self.subTest(destination=destination), self.assertRaisesRegex(ValueError, "overlap"):
                    cmz.stage_localized_app(src_app=source, dst_app=destination, resources=root, lang="zh-CN")
                self.assertEqual(marker.read_text(), "original")

    def test_latest_backup_uses_name_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            older = root / "Claude.backup-before-zh-CN-20261117-091011.app"
            newer = root / "Claude.backup-before-zh-TW-20261120-091011.app"
            older.mkdir()
            newer.mkdir()
            os.utime(older, (1800000000, 1800000000))
            os.utime(newer, (1700000000, 1700000000))
            self.assertEqual(cmz.find_latest_backup(root), newer)

    def test_status_cli_on_synthetic_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "Claude.app"
            (app / "Contents/Resources").mkdir(parents=True)
            with (app / "Contents/Info.plist").open("wb") as f:
                plistlib.dump(
                    {"CFBundleShortVersionString": "5.1.0", "CFBundleIdentifier": "com.example.claude"},
                    f,
                    fmt=plistlib.FMT_BINARY,
                )
            status = cmz.inspect_status(app, root, "zh-CN", home=root)
            self.assertEqual(status["version"], "5.1.0")
            self.assertEqual(status["profiles"], {})
            self.assertFalse(status["has_ion_dist_asar"])

    def test_apply_slash_menu_patch_updates_unpacked_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "Claude.app"
            res_dir = app / "Contents/Resources"
            res_dir.mkdir(parents=True)

            app_src = root / "app-src"
            (app_src / "native").mkdir(parents=True)
            (app_src / "main.js").write_text('const label = "Slash menu";', encoding="utf-8")
            (app_src / "native/ext.js").write_text('const ext = "Slash menu";', encoding="utf-8")
            app_asar = res_dir / "app.asar"
            cmz.build_asar(app_src, app_asar, {"native/ext.js"})

            plist_data = {
                "CFBundleShortVersionString": "5.1.0",
                "CFBundleIdentifier": "com.example.claude",
                "ElectronAsarIntegrity": {
                    "Resources/app.asar": {"algorithm": "SHA256", "hash": "0" * 64},
                },
            }
            with (app / "Contents/Info.plist").open("wb") as f:
                plistlib.dump(plist_data, f, fmt=plistlib.FMT_BINARY)

            dict_dir = root / "dicts"
            dict_dir.mkdir()
            (dict_dir / "slash-command-menu-patch.json").write_text(
                json.dumps({"zh-CN": {"Slash menu": "斜杠菜单"}}, ensure_ascii=False),
                encoding="utf-8",
            )

            applied = cmz.apply_slash_menu_patch(app, dict_dir, "zh-CN")
            self.assertTrue(applied)
            self.assertFalse((res_dir / "app.asar.new.unpacked").exists())
            self.assertFalse((res_dir / "app.asar.new").exists())
            self.assertEqual(
                (res_dir / "app.asar.unpacked/native/ext.js").read_text(encoding="utf-8"),
                'const ext = "斜杠菜单";',
            )
            raw = app_asar.read_bytes()
            hs, h_str, hdr = cmz.read_asar_header(raw, app_asar)
            self.assertEqual(
                cmz.read_asar_entry(raw, hs, hdr, "main.js"),
                'const label = "斜杠菜单";'.encode("utf-8"),
            )
            with (app / "Contents/Info.plist").open("rb") as f:
                updated_plist = plistlib.load(f)
            self.assertEqual(
                updated_plist["ElectronAsarIntegrity"]["Resources/app.asar"]["hash"],
                hashlib.sha256(h_str.encode("utf-8")).hexdigest(),
            )

    def test_inspect_status_reads_utf8_bom_config_and_cli_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "Claude.app"
            (app / "Contents/Resources").mkdir(parents=True)
            with (app / "Contents/Info.plist").open("wb") as f:
                plistlib.dump({"CFBundleShortVersionString": "5.2.0"}, f, fmt=plistlib.FMT_BINARY)
            cfg_dir = root / "Library/Application Support/Claude"
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "config.json").write_text('{"locale":"zh-CN"}', encoding="utf-8-sig")

            status = cmz.inspect_status(app, root, "zh-CN", home=root)
            self.assertEqual(status["profiles"]["Claude"], "zh-CN")

            script = Path(__file__).resolve().parent.parent / "claude_magpie_zh.py"
            proc = subprocess.run(
                [sys.executable, str(script), "status", "--app", str(app), "--backup-dir", str(root), "--json"],
                env={**os.environ, "HOME": str(root)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            parsed = json.loads(proc.stdout)
            self.assertEqual(parsed["profiles"]["Claude"], "zh-CN")
            self.assertEqual(parsed["version"], "5.2.0")

    def test_swap_creates_backup_dir_and_avoids_timestamp_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "Claude.app"
            (target / "Contents/MacOS").mkdir(parents=True)
            (target / "Contents/MacOS/Claude").write_text("orig", encoding="utf-8")

            staged = root / "staged/Claude.app"
            (staged / "Contents/MacOS").mkdir(parents=True)
            (staged / "Contents/MacOS/Claude").write_text("new", encoding="utf-8")

            backup_dir = root / "nested/backups"
            fixed_stamp = "20261121-120000"
            with (
                patch.object(cmz, "quit_claude_processes"),
                patch.object(cmz, "update_profile_locales", return_value=[]),
                patch.object(cmz.time, "strftime", return_value=fixed_stamp),
            ):
                b1 = cmz.swap_staged_into_place(
                    staged_app=staged,
                    target_app=target,
                    backup_dir=backup_dir,
                    lang="zh-CN",
                    relaunch_mode="none",
                )
                self.assertEqual(b1.name, f"Claude.backup-before-zh-CN-{fixed_stamp}.app")
                self.assertEqual((b1 / "Contents/MacOS/Claude").read_text(encoding="utf-8"), "orig")

                staged2 = root / "staged2/Claude.app"
                (staged2 / "Contents/MacOS").mkdir(parents=True)
                (staged2 / "Contents/MacOS/Claude").write_text("newer", encoding="utf-8")
                b2 = cmz.swap_staged_into_place(
                    staged_app=staged2,
                    target_app=target,
                    backup_dir=backup_dir,
                    lang="zh-CN",
                    relaunch_mode="none",
                )
                self.assertEqual(b2.name, f"Claude.backup-before-zh-CN-{fixed_stamp}-1.app")
                self.assertFalse((b1 / "Claude.app").exists())
                self.assertEqual(cmz.find_latest_backup(backup_dir), b2)

    def test_restore_refuses_overlapping_backup_and_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "Claude.app"
            target.mkdir()
            marker = target / "marker"
            marker.write_text("keep", encoding="utf-8")
            alias = root / "Alias.app"
            alias.symlink_to(target, target_is_directory=True)
            nested = target / "nested.app"
            nested.mkdir()

            for bad_backup in (target, alias, root, nested):
                with self.subTest(bad_backup=bad_backup), self.assertRaisesRegex(ValueError, "overlap"):
                    cmz.restore_backup(
                        target_app=target,
                        backup_dir=root,
                        backup_path=bad_backup,
                        relaunch_mode="none",
                    )
                self.assertEqual(marker.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
