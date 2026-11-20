#!/usr/bin/env python3
"""macOS Claude Desktop / Claude Magpie Chinese localization patcher.

Zero external Python dependencies (Python >= 3.10 stdlib only).
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from typing import Any

__version__ = "1.0.0"

SUPPORTED_LANGS = ("zh-CN", "zh-TW", "zh-HK")
DEFAULT_REPO_URL = "https://github.com/javaht/claude-desktop-zh-cn.git"
DEFAULT_APP_PATH = Path("/Applications/Claude.app")
ASAR_BLOCK_SIZE = 4 * 1024 * 1024
INTEGRITY_SENTINEL = b"AGbevlPCksUGKNL8TSn7wGmJEuJsXb2A"

FRONTEND_NEEDLE = '"en-US","de-DE","fr-FR","ko-KR","ja-JP"'
BASE_LANG_LIST = (
    '["en-US","de-DE","fr-FR","ko-KR","ja-JP",'
    '"es-419","es-ES","it-IT","hi-IN","pt-BR","id-ID"'
)
LANG_LIST_RE = re.compile(
    r'\["en-US","de-DE","fr-FR","ko-KR","ja-JP","es-419","es-ES","it-IT","hi-IN","pt-BR","id-ID"'
    r'(?:(?:,"zh-CN")|(?:,"zh-TW")|(?:,"zh-HK"))*\]'
)

STRUCTURAL_IDENT_RE = re.compile(r"[a-z][A-Za-z0-9_]*")
STRUCTURAL_SKIP = {
    '"Search"', "Search",
    '"Add"', "Add",
    '"Copy"', "Copy",
    '"Check"', "Check",
    '"Tasks"', "Tasks",
    '"Settings"', "Settings",
    '"Command"', "Command",
    '"Cancel"', "Cancel",
    '"Sort"', "Sort",
    '"Voice"', "Voice",
    '"Default"', "Default",
    '"Arguments"', "Arguments",
    '"Normal"', "Normal",
    '"All"', "All",
    '"Application"', "Application",
    '"Medium"', "Medium",
    '"Motion"', "Motion",
    '"Thinking"', "Thinking",
    '"Verbose"', "Verbose",
}

_COND = r"(?:(?:[^?:\"'`,;{}]|\"[^\"]{0,60}\"|'[^']{0,60}'|`[^`]{0,60}`){1,120}?)"
_QUOTED = r"(?:\"[^\"]{0,80}\"|'[^']{0,80}'|`[^`]{0,80}`)"
STRUCTURAL_CTX_RE = re.compile(
    r"(?<![A-Za-z0-9_$-])"
    r"(?:as|component|displayName|glyph|[\w$-]*[Ii]con(?:Name)?|name|role|type)"
    r"\s*[:=]\s*"
    r"(?:(?:" + _COND + r"\?\??\s*" + _QUOTED + r"\s*:\s*)*)"
    r"(?:" + _COND + r"(?:\?\??)?\s*)?(?:=\s*)?$"
)
STRUCTURAL_ALIAS_RE = re.compile(
    r"(?<![A-Za-z0-9_$-])[\w$-]*[Ii]con(?:Name)?\s*:\s*(?P<alias>[A-Za-z0-9_$]+)\b"
    r".*?"
    r"(?:=\s*(?P=alias)\s*===\s*void 0\s*\?\s*"
    r"|=\s*(?P=alias)\s*!==\s*void 0\s*\?\s*(?P=alias)\s*:\s*"
    r"|(?<![A-Za-z0-9_$-])(?P=alias)\s*\?\?\s*)$"
)

DISPLAY_PATCH_MARKER = "__claudeZhLabelPatch"
DISPLAY_PATCH_SNIPPET = (
    ";(()=>{const e=Intl.DisplayNames&&Intl.DisplayNames.prototype;"
    "if(!e||e.__claudeZhLabelPatch)return;"
    "const n=e.of;e.of=function(e){const t=String(e);"
    'return t==="zh-CN"?"简体中文":t==="zh-HK"?"繁體中文（香港）"'
    ':t==="zh-TW"?"繁體中文（台灣）":n.call(this,e)},'
    'Object.defineProperty(e,"__claudeZhLabelPatch",{value:!0})})();'
)

PROFILE_DIR_NAMES = ("Claude-Magpie-3p", "Claude-Magpie", "Claude-3p", "Claude")

_WORKER_RULES: list[tuple[str, str, re.Pattern[str] | None]] = []


def log(msg: str) -> None:
    print(msg, flush=True)


def run_cmd(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=check,
    )


def align4(n: int) -> int:
    return n + ((4 - (n % 4)) % 4)


def read_asar_header(data: bytes, path: Path) -> tuple[int, str, dict[str, Any]]:
    if len(data) < 16:
        raise RuntimeError(f"Invalid ASAR archive (too small): {path}")
    size_payload, header_size = struct.unpack_from("<II", data, 0)
    if size_payload != 4 or header_size <= 0 or len(data) < 8 + header_size:
        raise RuntimeError(f"Unsupported ASAR header in {path}")
    pickle = data[8 : 8 + header_size]
    payload_size, str_size = struct.unpack_from("<Ii", pickle, 0)
    if str_size < 0 or payload_size != align4(4 + str_size) or header_size != 4 + payload_size:
        raise RuntimeError(f"Unsupported ASAR pickle layout in {path}")
    header_str = pickle[8 : 8 + str_size].decode("utf-8")
    return header_size, header_str, json.loads(header_str)


def walk_asar(header: dict[str, Any]):
    stack: list[tuple[str, dict[str, Any]]] = [("", header)]
    while stack:
        prefix, node = stack.pop()
        files = node.get("files") if isinstance(node, dict) else None
        if not isinstance(files, dict):
            continue
        for name, child in files.items():
            if not isinstance(child, dict):
                continue
            rel = f"{prefix}/{name}" if prefix else name
            if "files" in child:
                stack.append((rel, child))
            else:
                yield rel, child


def read_asar_entry(
    data: bytes,
    header_size: int,
    header: dict[str, Any],
    rel: str,
) -> bytes | None:
    node: Any = header
    for part in rel.split("/"):
        files = node.get("files") if isinstance(node, dict) else None
        if not isinstance(files, dict) or part not in files:
            return None
        node = files[part]
    if not isinstance(node, dict) or "files" in node or node.get("unpacked"):
        return None
    offset = int(node["offset"])
    size = int(node["size"])
    start = 8 + header_size + offset
    return data[start : start + size]


def extract_asar(asar_path: Path, dst: Path) -> set[str]:
    data = asar_path.read_bytes()
    header_size, _, header = read_asar_header(data, asar_path)
    data_start = 8 + header_size
    unpacked_root = Path(str(asar_path) + ".unpacked")
    dst.mkdir(parents=True, exist_ok=True)
    root = dst.resolve()
    unpacked: set[str] = set()

    for rel, entry in walk_asar(header):
        parts = rel.split("/")
        if any(p in ("", ".", "..") or "\\" in p for p in parts):
            raise RuntimeError(f"Unsafe ASAR path rejected: {rel}")
        target = (dst / Path(*parts)).resolve()
        if target != root and root not in target.parents:
            raise RuntimeError(f"Escaping ASAR path rejected: {rel}")
        if entry.get("link"):
            raise RuntimeError(f"Symlink entry inside ASAR is unsupported: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.get("unpacked"):
            src_unpacked = unpacked_root / Path(*parts)
            if not src_unpacked.exists():
                raise RuntimeError(f"Missing unpacked ASAR file: {src_unpacked}")
            shutil.copy2(src_unpacked, target)
            unpacked.add(rel)
        else:
            off = int(entry["offset"])
            sz = int(entry["size"])
            target.write_bytes(data[data_start + off : data_start + off + sz])
    return unpacked


def compute_file_integrity(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    blocks = [
        hashlib.sha256(raw[i : i + ASAR_BLOCK_SIZE]).hexdigest()
        for i in range(0, len(raw), ASAR_BLOCK_SIZE)
    ] or [hashlib.sha256(b"").hexdigest()]
    return {
        "algorithm": "SHA256",
        "hash": hashlib.sha256(raw).hexdigest(),
        "blockSize": ASAR_BLOCK_SIZE,
        "blocks": blocks,
    }


def build_asar(src_dir: Path, dst: Path, unpacked: set[str]) -> None:
    entries: list[tuple[Path, str, dict[str, Any]]] = []

    def walk(directory: Path, node: dict[str, Any]) -> None:
        for item in sorted(directory.iterdir(), key=lambda p: p.name):
            if item.is_symlink():
                continue
            if item.is_dir():
                child: dict[str, Any] = {"files": {}}
                node["files"][item.name] = child
                walk(item, child)
            elif item.is_file():
                rel = str(item.relative_to(src_dir)).replace(os.sep, "/")
                ent: dict[str, Any] = {"size": item.stat().st_size}
                node["files"][item.name] = ent
                entries.append((item, rel, ent))

    header: dict[str, Any] = {"files": {}}
    walk(src_dir, header)
    offset = 0
    for path_obj, rel, ent in entries:
        ent["offset"] = str(offset)
        ent["integrity"] = compute_file_integrity(path_obj)
        if rel in unpacked:
            ent["unpacked"] = True
        else:
            offset += int(ent["size"])

    h_bytes = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    p_size = align4(4 + len(h_bytes))
    h_size = 4 + p_size
    prefix = struct.pack("<II", 4, h_size)
    padding = b"\x00" * (p_size - 4 - len(h_bytes))
    payload = struct.pack("<II", p_size, len(h_bytes)) + h_bytes + padding

    tmp = dst.with_name(dst.name + ".tmp")
    with tmp.open("wb") as out:
        out.write(prefix)
        out.write(payload)
        for path_obj, rel, _ in entries:
            if rel not in unpacked:
                with path_obj.open("rb") as f:
                    shutil.copyfileobj(f, out)
    os.replace(tmp, dst)

    if unpacked:
        u_root = Path(str(dst) + ".unpacked")
        u_root.mkdir(parents=True, exist_ok=True)
        for rel in sorted(unpacked):
            tgt = u_root / Path(*rel.split("/"))
            tgt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_dir / Path(*rel.split("/")), tgt)


def find_assets_dir(ion_dist: Path) -> Path:
    known = ion_dist / "assets/v1"
    if any(known.glob("*.js")):
        return known
    for js in sorted(ion_dist.rglob("*.js")):
        try:
            if FRONTEND_NEEDLE in js.read_text(encoding="utf-8"):
                return js.parent
        except (OSError, UnicodeDecodeError):
            continue
    raise RuntimeError("Cannot locate frontend JS assets directory inside ion-dist")


def _is_plain_ui(s: str) -> bool:
    return "\n" not in s and not any(c in s for c in ('"', "\\", "=", ";", "=>"))


def compile_hardcoded_rules(
    rules_raw: list[tuple[str, str]],
) -> list[tuple[str, str, re.Pattern[str] | None]]:
    ordered = sorted(rules_raw, key=lambda x: len(x[0]), reverse=True)
    compiled: list[tuple[str, str, re.Pattern[str] | None]] = []
    for src, tgt in ordered:
        if STRUCTURAL_IDENT_RE.fullmatch(src) or src in STRUCTURAL_SKIP:
            continue
        pat = (
            re.compile(r'(?P<quote>["\'`])' + re.escape(src) + r"(?P=quote)")
            if _is_plain_ui(src)
            else None
        )
        compiled.append((src, tgt, pat))
    return compiled


def _init_worker(rules_raw: list[tuple[str, str]]) -> None:
    global _WORKER_RULES
    _WORKER_RULES = compile_hardcoded_rules(rules_raw)


def in_structural_context(text: str, pos: int) -> bool:
    pre = text[max(0, pos - 160) : pos]
    if STRUCTURAL_ALIAS_RE.search(pre):
        return True
    short = pre[-96:] if len(pre) > 96 else pre
    return STRUCTURAL_CTX_RE.search(short) is not None


def apply_rules_to_js_text(
    text: str,
    rules: list[tuple[str, str, re.Pattern[str] | None]],
) -> tuple[str, int]:
    if not any(src in text for src, _, _ in rules):
        return text, 0
    cur = text
    total = 0
    for src, tgt, pat in rules:
        if src not in cur:
            continue
        if pat is None:
            cnt = cur.count(src)
            if cnt:
                cur = cur.replace(src, tgt)
                total += cnt
        else:
            cnt = 0

            def repl(m: re.Match[str]) -> str:
                nonlocal cnt
                if in_structural_context(cur, m.start()):
                    return m.group(0)
                cnt += 1
                q = m.group("quote")
                return f"{q}{tgt}{q}"

            cur = pat.sub(repl, cur)
            total += cnt
    return cur, total


def _patch_one_js(path_str: str) -> tuple[bool, int]:
    path = Path(path_str)
    text = path.read_text(encoding="utf-8")
    updated, count = apply_rules_to_js_text(text, _WORKER_RULES)
    if updated != text:
        path.write_text(updated, encoding="utf-8")
        return True, count
    return False, 0


def patch_language_whitelist(js_files: list[Path], lang: str) -> int:
    replacement = f'{BASE_LANG_LIST},"{lang}"]'
    hits = 0
    for path in js_files:
        txt = path.read_text(encoding="utf-8")
        if LANG_LIST_RE.search(txt):
            new_txt, occ = LANG_LIST_RE.subn(replacement, txt)
            if occ:
                path.write_text(new_txt, encoding="utf-8")
                hits += occ
                log(f"  whitelist patched: {path.name} ({occ})")
    return hits


def patch_display_names(js_files: list[Path]) -> int:
    patched = 0
    for path in js_files:
        txt = path.read_text(encoding="utf-8")
        if DISPLAY_PATCH_MARKER not in txt:
            path.write_text(txt + DISPLAY_PATCH_SNIPPET, encoding="utf-8")
            patched += 1
            log(f"  display name patch applied: {path.name}")
    return patched


def merge_i18n_catalog(en_path: Path, zh_src_path: Path, out_path: Path) -> tuple[int, int]:
    en_json = json.loads(en_path.read_text(encoding="utf-8"))
    zh_json = json.loads(zh_src_path.read_text(encoding="utf-8"))
    merged: dict[str, Any] = {}
    translated = 0
    fallback = 0
    for key, en_val in en_json.items():
        if key in zh_json:
            merged[key] = zh_json[key]
            if zh_json[key] != en_val:
                translated += 1
        else:
            merged[key] = en_val
            fallback += 1
    out_path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return translated, fallback


def patch_ion_dist(
    ion_dist: Path,
    resources: Path,
    lang: str,
    *,
    workers: int | None = None,
) -> dict[str, int]:
    assets_dir = find_assets_dir(ion_dist)
    js_files = sorted(assets_dir.glob("*.js"))

    wl_hits = patch_language_whitelist(js_files, lang)
    if wl_hits == 0:
        raise RuntimeError(
            "Language whitelist pattern not found in frontend JS; "
            "Claude Desktop may have changed its locale list format."
        )

    hardcoded_file = resources / f"frontend-hardcoded-{lang}.json"
    rules_raw: list[tuple[str, str]] = [
        (a, b) for a, b in json.loads(hardcoded_file.read_text(encoding="utf-8"))
    ]

    worker_count = workers if workers is not None else min(os.cpu_count() or 1, 10)
    by_size = sorted(js_files, key=lambda p: p.stat().st_size, reverse=True)
    patched_files = 0
    patched_count = 0
    if worker_count <= 1 or len(by_size) <= 4:
        compiled = compile_hardcoded_rules(rules_raw)
        for path in by_size:
            orig = path.read_text(encoding="utf-8")
            updated, cnt = apply_rules_to_js_text(orig, compiled)
            if updated != orig:
                path.write_text(updated, encoding="utf-8")
                patched_files += 1
                patched_count += cnt
    else:
        with ProcessPoolExecutor(
            max_workers=worker_count,
            initializer=_init_worker,
            initargs=(rules_raw,),
        ) as pool:
            for changed, cnt in pool.map(_patch_one_js, (str(p) for p in by_size), chunksize=1):
                patched_files += int(changed)
                patched_count += cnt
    log(f"  hardcoded JS strings replaced: {patched_count} across {patched_files} files")

    index_files = sorted(assets_dir.glob("index-*.js"))
    patch_display_names(index_files)

    translated, fallback = merge_i18n_catalog(
        ion_dist / "i18n/en-US.json",
        resources / f"frontend-{lang}.json",
        ion_dist / f"i18n/{lang}.json",
    )
    log(f"  frontend i18n merged: {translated} translated, {fallback} English fallback")

    statsig_dir = ion_dist / "i18n/statsig"
    if statsig_dir.is_dir():
        src = resources / f"statsig-{lang}.json"
        if not src.exists():
            src = statsig_dir / "en-US.json"
        if src.exists():
            shutil.copy2(src, statsig_dir / f"{lang}.json")
            log("  installed statsig catalog")

    dyn_dir = ion_dist / "i18n/dynamic"
    if (dyn_dir / "en-US.json").exists():
        src = resources / f"dynamic-{lang}.json"
        if not src.exists():
            src = dyn_dir / "en-US.json"
        if src.exists():
            shutil.copy2(src, dyn_dir / f"{lang}.json")
            log("  installed dynamic catalog")

    return {
        "whitelist_hits": wl_hits,
        "hardcoded_replacements": patched_count,
        "hardcoded_files": patched_files,
        "i18n_translated": translated,
        "i18n_fallback": fallback,
    }


def digest_integrity_dict(integrity: dict[str, Any]) -> str:
    h = hashlib.sha256()
    for k in sorted(integrity):
        entry = integrity[k]
        h.update(k.encode("utf-8"))
        h.update(str(entry.get("algorithm", "")).encode("utf-8"))
        h.update(str(entry.get("hash", "")).encode("utf-8"))
    return h.hexdigest()


def patch_framework_integrity_bytes(fw_bytes: bytes, digest_hex: str) -> tuple[bytes, int]:
    data = bytearray(fw_bytes)
    digest = bytes.fromhex(digest_hex)
    if len(digest) != 32:
        raise ValueError("Integrity digest must be 32 bytes (64 hex chars)")
    start = 0
    slots = 0
    while True:
        idx = data.find(INTEGRITY_SENTINEL, start)
        if idx < 0:
            break
        slot = idx + len(INTEGRITY_SENTINEL)
        if slot + 34 <= len(data) and data[slot] == 1:
            data[slot + 1] = 1
            data[slot + 2 : slot + 34] = digest
            slots += 1
        start = idx + 1
    return bytes(data), slots


def update_asar_integrity(app: Path, key: str, asar_path: Path) -> int:
    plist_path = app / "Contents/Info.plist"
    with plist_path.open("rb") as f:
        info = plistlib.load(f)
    integrity = info.get("ElectronAsarIntegrity")
    if not isinstance(integrity, dict) or key not in integrity:
        raise RuntimeError(f"ElectronAsarIntegrity missing key: {key}")

    _, h_str, _ = read_asar_header(asar_path.read_bytes(), asar_path)
    integrity[key]["hash"] = hashlib.sha256(h_str.encode("utf-8")).hexdigest()
    with plist_path.open("wb") as f:
        plistlib.dump(info, f, fmt=plistlib.FMT_BINARY, sort_keys=False)

    fw_path = app / "Contents/Frameworks/Electron Framework.framework/Versions/A/Electron Framework"
    if not fw_path.exists():
        log("  Electron Framework binary not found; skipped sentinel slot update")
        return 0
    patched_fw, slots = patch_framework_integrity_bytes(
        fw_path.read_bytes(),
        digest_integrity_dict(integrity),
    )
    if slots:
        fw_path.write_bytes(patched_fw)
    log(f"  updated Electron integrity slot(s): {slots}")
    return slots


def load_entitlements(path: Path) -> dict[str, Any]:
    res = subprocess.run(
        ["codesign", "-d", "--entitlements", ":-", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    raw = res.stdout.rstrip(b"\x00")
    if res.returncode != 0 or not raw.strip():
        return {}
    try:
        loaded = plistlib.loads(raw)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def sign_path(path: Path, ent_dir: Path, dr: str | None = None) -> None:
    ent = load_entitlements(path)
    if ent:
        ent.pop("com.apple.application-identifier", None)
        ent.pop("com.apple.developer.team-identifier", None)
        ent.pop("keychain-access-groups", None)
        ent["com.apple.security.cs.disable-library-validation"] = True
    cmd = [
        "codesign",
        "--force",
        "--sign",
        "-",
        "--timestamp=none",
        "--options",
        "runtime",
        "--preserve-metadata=identifier,flags",
    ]
    if ent:
        digest = hashlib.sha256(path.as_posix().encode("utf-8")).hexdigest()[:16]
        ep = ent_dir / f"{digest}.plist"
        ep.write_bytes(plistlib.dumps(ent, fmt=plistlib.FMT_XML))
        cmd.extend(["--entitlements", str(ep)])
    if dr:
        cmd.extend(["-r", f"=designated => {dr}"])
    cmd.append(str(path))
    res = run_cmd(cmd, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"codesign failed on {path}:\n{res.stdout}")


def resign_app(app: Path) -> None:
    ent_dir = Path(tempfile.mkdtemp(prefix="claude-magpie-zh-ent."))
    try:
        files: list[Path] = []
        bundles: list[Path] = []
        for root, dirs, fnames in os.walk(app / "Contents"):
            rp = Path(root)
            for d in dirs:
                p = rp / d
                if p.suffix in {".app", ".framework"}:
                    bundles.append(p)
            for fn in fnames:
                p = rp / fn
                if (
                    not p.is_symlink()
                    and p.is_file()
                    and (p.suffix in {".dylib", ".node", ".so"} or os.access(p, os.X_OK))
                ):
                    files.append(p)
        log(f"  signing {len(files)} binaries and {len(bundles) + 1} bundles...")
        for p in sorted(files, key=lambda x: len(x.parts), reverse=True):
            sign_path(p, ent_dir)
        for p in sorted(bundles, key=lambda x: len(x.parts), reverse=True):
            sign_path(p, ent_dir)
        with (app / "Contents/Info.plist").open("rb") as f:
            bid = plistlib.load(f).get("CFBundleIdentifier")
        sign_path(app, ent_dir, dr=f'identifier "{bid}"' if bid else None)
    finally:
        shutil.rmtree(ent_dir, ignore_errors=True)


def verify_app(app: Path, lang: str, *, check_codesign: bool = True) -> dict[str, Any]:
    asar = app / "Contents/Resources/ion-dist.asar"
    if not asar.exists():
        raise RuntimeError(f"Missing ion-dist.asar at {asar}")

    data = asar.read_bytes()
    hs, _, hdr = read_asar_header(data, asar)
    raw_i18n = read_asar_entry(data, hs, hdr, f"i18n/{lang}.json")
    if not raw_i18n:
        raise RuntimeError(f"Missing {lang} catalog in rebuilt ion-dist.asar")
    vals = [v for v in json.loads(raw_i18n.decode("utf-8")).values() if isinstance(v, str)]
    cn_count = sum(1 for v in vals if re.search(r"[一-鿿]", v))
    log(f"  verified ion-dist.asar i18n/{lang}.json: {cn_count}/{len(vals)} Chinese strings")

    if read_asar_entry(data, hs, hdr, f"i18n/dynamic/{lang}.json") is None:
        raise RuntimeError(f"Missing dynamic {lang} catalog")

    with (app / "Contents/Info.plist").open("rb") as f:
        info = plistlib.load(f)
    integrity = info.get("ElectronAsarIntegrity", {})
    for key in ("Resources/app.asar", "Resources/ion-dist.asar"):
        p = app / "Contents" / key
        if key not in integrity or not p.exists():
            continue
        _, s, _ = read_asar_header(p.read_bytes(), p)
        exp = hashlib.sha256(s.encode("utf-8")).hexdigest()
        act = integrity[key]["hash"]
        if act != exp:
            raise RuntimeError(f"{key} hash mismatch: {act} != {exp}")

    fw_path = app / "Contents/Frameworks/Electron Framework.framework/Versions/A/Electron Framework"
    if fw_path.exists():
        fw = fw_path.read_bytes()
        exp_dig = digest_integrity_dict(integrity)
        idx = fw.find(INTEGRITY_SENTINEL)
        while idx >= 0:
            slot = idx + len(INTEGRITY_SENTINEL)
            if slot + 34 <= len(fw) and fw[slot] == 1:
                act_dig = fw[slot + 2 : slot + 34].hex()
                if act_dig != exp_dig:
                    raise RuntimeError("Electron Framework integrity digest mismatch")
            idx = fw.find(INTEGRITY_SENTINEL, idx + 1)
    log("  verified ElectronAsarIntegrity + Electron Framework digest")

    if check_codesign:
        cs = run_cmd(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)], check=False)
        if cs.returncode != 0:
            raise RuntimeError(f"codesign verify failed:\n{cs.stdout}")
        ent = run_cmd(["codesign", "-d", "--entitlements", "-", str(app)], check=False).stdout
        if "com.apple.security.virtualization" not in ent:
            log("  warning: com.apple.security.virtualization entitlement not found on app")
        log("  verified codesign --verify --deep --strict")

    return {
        "version": info.get("CFBundleShortVersionString", "unknown"),
        "cn_strings": cn_count,
        "total_strings": len(vals),
    }


def apply_slash_menu_patch(app: Path, resources: Path, lang: str) -> bool:
    patch_file = resources / "slash-command-menu-patch.json"
    if not patch_file.exists():
        return False
    app_asar = app / "Contents/Resources/app.asar"
    if not app_asar.exists():
        return False

    patch_spec = json.loads(patch_file.read_text(encoding="utf-8"))
    lang_map = patch_spec.get(lang) or patch_spec.get("zh-CN")
    if not isinstance(lang_map, dict) or not lang_map:
        return False

    tmp_dir = Path(tempfile.mkdtemp(prefix="claude-magpie-zh-app."))
    try:
        unpacked = extract_asar(app_asar, tmp_dir)
        replacements = 0
        for js in tmp_dir.rglob("*.js"):
            txt = js.read_text(encoding="utf-8", errors="replace")
            cur = txt
            for src, tgt in lang_map.items():
                if src in cur:
                    cur = cur.replace(src, tgt)
                    replacements += 1
            if cur != txt:
                js.write_text(cur, encoding="utf-8")
        if replacements == 0:
            return False
        tmp_asar = app_asar.with_name("app.asar.new")
        build_asar(tmp_dir, tmp_asar, unpacked)
        os.replace(tmp_asar, app_asar)
        update_asar_integrity(app, "Resources/app.asar", app_asar)
        log(f"  applied slash-command menu patch ({replacements} rule hits)")
        return True
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def stage_localized_app(
    *,
    src_app: Path,
    dst_app: Path,
    resources: Path,
    lang: str,
    include_slash_menu: bool = False,
    workers: int | None = None,
) -> dict[str, Any]:
    if lang not in SUPPORTED_LANGS:
        raise ValueError(f"Unsupported language '{lang}'; choose from {SUPPORTED_LANGS}")
    if not src_app.exists():
        raise FileNotFoundError(f"Source app not found: {src_app}")

    source = src_app.resolve()
    destination = dst_app.resolve()
    if (
        destination.is_relative_to(source) or source.is_relative_to(destination)
        or (dst_app.exists() and src_app.samefile(dst_app))
    ):
        raise ValueError(f"Source and staging apps must not overlap: {src_app} -> {dst_app}")

    t0 = time.perf_counter()
    if dst_app.exists():
        shutil.rmtree(dst_app)
    dst_app.parent.mkdir(parents=True, exist_ok=True)
    log(f"1. Copying {src_app} -> {dst_app}")
    run_cmd(["ditto", str(src_app), str(dst_app)])

    res_dir = dst_app / "Contents/Resources"
    ion_dir = res_dir / "ion-dist"
    ion_asar = res_dir / "ion-dist.asar"
    if not ion_asar.exists():
        raise RuntimeError(
            f"Missing {ion_asar}. This tool supports Claude Desktop bundles using ion-dist.asar."
        )
    if ion_dir.exists():
        if any(ion_dir.rglob("*.js")):
            raise RuntimeError("Unexpected JS files in placeholder ion-dist directory")
        shutil.rmtree(ion_dir)

    log("2. Extracting ion-dist.asar")
    unpacked = extract_asar(ion_asar, ion_dir)
    try:
        log(f"3. Applying {lang} localization")
        stats = patch_ion_dist(ion_dir, resources, lang, workers=workers)

        desktop_json = resources / f"desktop-{lang}.json"
        if desktop_json.exists():
            shutil.copy2(desktop_json, res_dir / f"{lang}.json")

        loc_str = resources / (
            f"Localizable-{lang}.strings"
            if (resources / f"Localizable-{lang}.strings").exists()
            else "Localizable.strings"
        )
        if loc_str.exists():
            for folder in (f"{lang}.lproj", f"{lang.replace('-', '_')}.lproj"):
                d = res_dir / folder
                d.mkdir(parents=True, exist_ok=True)
                shutil.copy2(loc_str, d / "Localizable.strings")

        log("4. Rebuilding ion-dist.asar")
        tmp_asar = ion_asar.with_name("ion-dist.asar.new")
        build_asar(ion_dir, tmp_asar, unpacked)
        os.replace(tmp_asar, ion_asar)
    finally:
        shutil.rmtree(ion_dir, ignore_errors=True)

    if include_slash_menu:
        apply_slash_menu_patch(dst_app, resources, lang)

    log("5. Syncing Electron integrity + re-signing")
    update_asar_integrity(dst_app, "Resources/ion-dist.asar", ion_asar)
    resign_app(dst_app)
    run_cmd(["xattr", "-dr", "com.apple.quarantine", str(dst_app)], check=False)

    log("6. Verifying staged app")
    verify_info = verify_app(dst_app, lang, check_codesign=True)
    elapsed = round(time.perf_counter() - t0, 2)
    log(f"Stage build succeeded in {elapsed:.1f}s")
    return {**stats, **verify_info, "elapsed_sec": elapsed}


def discover_profile_dirs(home: Path | None = None) -> list[Path]:
    base = (home or Path.home()) / "Library/Application Support"
    found: list[Path] = []
    for name in PROFILE_DIR_NAMES:
        d = base / name
        if d.is_dir():
            found.append(d)
    return found


def update_profile_locales(lang: str, *, home: Path | None = None) -> list[Path]:
    updated: list[Path] = []
    for d in discover_profile_dirs(home):
        cfg = d / "config.json"
        data: dict[str, Any] = {}
        mode = None
        try:
            target = cfg.resolve()
            if target.exists():
                loaded = json.loads(target.read_text(encoding="utf-8-sig"))
                if not isinstance(loaded, dict):
                    raise ValueError("expected a JSON object")
                data = loaded
                mode = target.stat().st_mode & 0o777
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"WARNING: Preserving {cfg}; unable to read its settings: {exc}", file=sys.stderr)
            continue

        data["locale"] = lang
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=target.parent, delete=False
            ) as handle:
                tmp = Path(handle.name)
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            if mode is not None:
                tmp.chmod(mode)
            tmp.replace(target)
        except OSError as exc:
            print(f"WARNING: Unable to update locale in {cfg}: {exc}", file=sys.stderr)
            continue
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
        updated.append(cfg)
    return updated


def resolve_relaunch_target(mode: str, app_path: Path, home: Path | None = None) -> Path | None:
    if mode == "none":
        return None
    h = home or Path.home()
    magpie_candidates = [
        h / "Applications/Claude Magpie.app",
        Path("/Applications/Claude Magpie.app"),
    ]
    if mode == "magpie":
        for cand in magpie_candidates:
            if cand.exists():
                return cand
        return None
    if mode == "claude":
        return app_path if app_path.exists() else None
    for cand in magpie_candidates:
        if cand.exists():
            return cand
    return app_path if app_path.exists() else None


def resolve_resources_dir(
    explicit: Path | None,
    repo_url: str,
) -> tuple[Path, Path | None]:
    if explicit is not None:
        res = explicit.expanduser().resolve()
        if (res / "resources").is_dir() and not any(res.glob("frontend-*.json")):
            res = res / "resources"
        if not res.is_dir():
            raise FileNotFoundError(f"Resources directory not found: {res}")
        return res, None

    tmp_clone = Path(tempfile.mkdtemp(prefix="claude-magpie-zh-dict."))
    log(f"Fetching localization dictionaries from {repo_url} ...")
    res = run_cmd(
        ["git", "clone", "--depth", "1", repo_url, str(tmp_clone)],
        check=False,
    )
    if res.returncode != 0:
        shutil.rmtree(tmp_clone, ignore_errors=True)
        raise RuntimeError(f"git clone failed:\n{res.stdout}")
    return tmp_clone / "resources", tmp_clone


def quit_claude_processes(app_path: Path) -> None:
    run_cmd(["osascript", "-e", 'tell application "Claude" to quit'], check=False)
    pattern = f"{app_path}/Contents/"
    for i in range(1, 21):
        chk = subprocess.run(["pgrep", "-f", pattern], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if chk.returncode != 0:
            break
        if i == 10:
            log("Sending SIGTERM to remaining Claude processes...")
            subprocess.run(["pkill", "-TERM", "-f", pattern], check=False)
        elif i == 16:
            log("Sending SIGKILL to stubborn Claude processes...")
            subprocess.run(["pkill", "-KILL", "-f", pattern], check=False)
        time.sleep(0.5)


def swap_staged_into_place(
    *,
    staged_app: Path,
    target_app: Path,
    backup_dir: Path,
    lang: str,
    relaunch_mode: str,
) -> Path:
    if not (staged_app / "Contents/MacOS/Claude").exists():
        raise RuntimeError(f"Staged app binary missing: {staged_app}")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"Claude.backup-before-{lang}-{stamp}.app"

    quit_claude_processes(target_app)

    log(f"Backing up {target_app} -> {backup_path}")
    shutil.move(str(target_app), str(backup_path))
    try:
        shutil.move(str(staged_app), str(target_app))
    except Exception:
        log("ERROR moving staged app into place; rolling back backup!")
        if not target_app.exists() and backup_path.exists():
            shutil.move(str(backup_path), str(target_app))
        raise

    updated_cfgs = update_profile_locales(lang)
    for p in updated_cfgs:
        log(f"Updated {p} -> locale={lang}")

    launch_target = resolve_relaunch_target(relaunch_mode, target_app)
    if launch_target is not None:
        log(f"Launching {launch_target}...")
        run_cmd(["open", "-a", str(launch_target)], check=False)
    return backup_path


def schedule_delayed_swap(
    *,
    staged_app: Path,
    target_app: Path,
    backup_dir: Path,
    lang: str,
    relaunch_mode: str,
    delay_sec: int,
) -> Path:
    log_path = Path(tempfile.gettempdir()) / "claude-magpie-zh-swap.log"
    script_path = Path(tempfile.gettempdir()) / "claude-magpie-zh-swap.py"
    launch_target = resolve_relaunch_target(relaunch_mode, target_app)
    payload = f"""#!/usr/bin/env python3
import time
from pathlib import Path
import sys
sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})
import claude_magpie_zh as mod

time.sleep({int(delay_sec)})
mod.swap_staged_into_place(
    staged_app=Path({str(staged_app)!r}),
    target_app=Path({str(target_app)!r}),
    backup_dir=Path({str(backup_dir)!r}),
    lang={lang!r},
    relaunch_mode={relaunch_mode!r},
)
"""
    script_path.write_text(payload, encoding="utf-8")
    os.chmod(script_path, 0o755)
    with log_path.open("w", encoding="utf-8") as lf:
        subprocess.Popen(
            [sys.executable, str(script_path)],
            stdin=subprocess.DEVNULL,
            stdout=lf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    log(
        f"Scheduled detached swap in {delay_sec}s "
        f"(launch={launch_target}, log={log_path})"
    )
    return log_path


def find_latest_backup(backup_dir: Path) -> Path | None:
    def backup_time(path: Path) -> float:
        stamp = re.search(r"-(\d{8}-\d{6})\.app$", path.name)
        if stamp:
            try:
                return time.mktime(time.strptime(stamp.group(1), "%Y%m%d-%H%M%S"))
            except ValueError:
                pass  # Older or manually named backups can still use mtime.
        return path.stat().st_mtime

    candidates = (p for p in backup_dir.glob("Claude.backup-before-*.app") if p.is_dir())
    return max(candidates, key=backup_time, default=None)


def restore_backup(
    *,
    target_app: Path,
    backup_dir: Path,
    backup_path: Path | None,
    relaunch_mode: str,
) -> Path:
    chosen = backup_path or find_latest_backup(backup_dir)
    if chosen is None or not chosen.is_dir():
        raise FileNotFoundError(
            f"No backup found matching {backup_dir}/Claude.backup-before-*.app"
        )

    quit_claude_processes(target_app)

    removing = target_app.with_name(f"Claude.zh-removing-{time.strftime('%Y%m%d-%H%M%S')}.app")
    if target_app.exists():
        shutil.move(str(target_app), str(removing))
    try:
        shutil.move(str(chosen), str(target_app))
    except Exception:
        if not target_app.exists() and removing.exists():
            shutil.move(str(removing), str(target_app))
        raise
    if removing.exists():
        shutil.rmtree(removing, ignore_errors=True)

    for p in update_profile_locales("en-US"):
        log(f"Reset {p} -> locale=en-US")

    launch_target = resolve_relaunch_target(relaunch_mode, target_app)
    if launch_target is not None:
        log(f"Launching {launch_target}...")
        run_cmd(["open", "-a", str(launch_target)], check=False)
    return chosen


def inspect_status(
    app_path: Path,
    backup_dir: Path,
    lang: str,
    *,
    home: Path | None = None,
) -> dict[str, Any]:
    info: dict[str, Any] = {
        "app_path": str(app_path),
        "app_exists": app_path.exists(),
        "lang": lang,
        "backups": [str(p) for p in sorted(backup_dir.glob("Claude.backup-before-*.app"))],
        "profiles": {},
    }
    for d in discover_profile_dirs(home):
        cfg = d / "config.json"
        locale_val = None
        if cfg.exists():
            try:
                locale_val = json.loads(cfg.read_text(encoding="utf-8")).get("locale")
            except Exception:
                locale_val = "<unreadable>"
        info["profiles"][d.name] = locale_val

    if app_path.exists():
        plist_path = app_path / "Contents/Info.plist"
        if plist_path.exists():
            with plist_path.open("rb") as f:
                pl = plistlib.load(f)
            info["version"] = pl.get("CFBundleShortVersionString")
            info["bundle_id"] = pl.get("CFBundleIdentifier")
        asar = app_path / "Contents/Resources/ion-dist.asar"
        info["has_ion_dist_asar"] = asar.exists()
        if asar.exists():
            data = asar.read_bytes()
            hs, _, hdr = read_asar_header(data, asar)
            info["has_lang_catalog"] = read_asar_entry(data, hs, hdr, f"i18n/{lang}.json") is not None
    return info


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="claude_magpie_zh",
        description="Localize macOS Claude Desktop / Claude Magpie frontend to Chinese.",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    p_inst = sub.add_parser("install", help="Stage, patch, sign, and optionally swap Claude.app")
    p_inst.add_argument("--lang", default="zh-CN", choices=SUPPORTED_LANGS)
    p_inst.add_argument("--app", type=Path, default=DEFAULT_APP_PATH)
    p_inst.add_argument("--resources", type=Path, default=None, help="Local claude-desktop-zh-cn resources dir")
    p_inst.add_argument("--repo-url", default=DEFAULT_REPO_URL)
    p_inst.add_argument("--stage-dir", type=Path, default=None)
    p_inst.add_argument("--stage-only", action="store_true", help="Build & verify staged app without replacing --app")
    p_inst.add_argument("--delayed-swap", type=int, default=0, metavar="SECONDS", help="Run quit/swap/relaunch detached after N seconds")
    p_inst.add_argument("--backup-dir", type=Path, default=Path("/Applications"))
    p_inst.add_argument("--relaunch", default="auto", choices=("auto", "magpie", "claude", "none"))
    p_inst.add_argument("--include-slash-menu", action="store_true")
    p_inst.add_argument("--workers", type=int, default=None)

    p_rest = sub.add_parser("restore", help="Restore the latest official Claude.app backup")
    p_rest.add_argument("--app", type=Path, default=DEFAULT_APP_PATH)
    p_rest.add_argument("--backup-dir", type=Path, default=Path("/Applications"))
    p_rest.add_argument("--backup", type=Path, default=None)
    p_rest.add_argument("--relaunch", default="auto", choices=("auto", "magpie", "claude", "none"))

    p_stat = sub.add_parser("status", help="Inspect Claude.app localization & backup status")
    p_stat.add_argument("--app", type=Path, default=DEFAULT_APP_PATH)
    p_stat.add_argument("--backup-dir", type=Path, default=Path("/Applications"))
    p_stat.add_argument("--lang", default="zh-CN", choices=SUPPORTED_LANGS)
    p_stat.add_argument("--json", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 10):
        raise SystemExit("Python >= 3.10 is required.")

    args = build_parser().parse_args(argv)
    if args.command == "status":
        status = inspect_status(args.app.expanduser(), args.backup_dir.expanduser(), args.lang)
        if args.json:
            print(json.dumps(status, ensure_ascii=False, indent=2))
        else:
            for k, v in status.items():
                print(f"{k}: {v}")
        return 0

    if args.command == "restore":
        restored = restore_backup(
            target_app=args.app.expanduser(),
            backup_dir=args.backup_dir.expanduser(),
            backup_path=args.backup.expanduser() if args.backup else None,
            relaunch_mode=args.relaunch,
        )
        log(f"Restored official Claude.app from {restored}")
        return 0

    if args.command == "install":
        res_dir, temp_clone = resolve_resources_dir(args.resources, args.repo_url)
        stage_root = (
            args.stage_dir.expanduser().resolve()
            if args.stage_dir
            else Path(tempfile.mkdtemp(prefix="claude-magpie-zh-stage."))
        )
        staged_app = stage_root / "Claude.app"
        try:
            stage_localized_app(
                src_app=args.app.expanduser(),
                dst_app=staged_app,
                resources=res_dir,
                lang=args.lang,
                include_slash_menu=args.include_slash_menu,
                workers=args.workers,
            )
        finally:
            if temp_clone is not None:
                shutil.rmtree(temp_clone, ignore_errors=True)

        if args.stage_only:
            log(f"Stage-only complete. Verified bundle at: {staged_app}")
            return 0

        if args.delayed_swap > 0:
            schedule_delayed_swap(
                staged_app=staged_app,
                target_app=args.app.expanduser(),
                backup_dir=args.backup_dir.expanduser(),
                lang=args.lang,
                relaunch_mode=args.relaunch,
                delay_sec=args.delayed_swap,
            )
            return 0

        backup_path = swap_staged_into_place(
            staged_app=staged_app,
            target_app=args.app.expanduser(),
            backup_dir=args.backup_dir.expanduser(),
            lang=args.lang,
            relaunch_mode=args.relaunch,
        )
        if args.stage_dir is None:
            shutil.rmtree(stage_root, ignore_errors=True)
        log(f"Localization complete! Backup saved at: {backup_path}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
