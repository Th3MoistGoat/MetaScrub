"""
scrub_core.py - metadata engine for MetaScrub.

Wraps a bundled copy of ExifTool (Phil Harvey) for reading/writing metadata
across images, audio, video and documents, and adds:

  * ICC color-profile preservation: the profile is extracted before the
    scrub and written back afterwards, so colors don't shift.
  * Extra OOXML (docx/xlsx/pptx) sanitizing done in pure Python, since
    author/revision/comment data can hide in several zip parts.

Only the standard library is used here; the GUI lives in MetaScrub.py.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from xml.etree import ElementTree as ET

from audio_scrub import strip_id3_tags, strip_flac_tags, strip_ogg_tags

# ---------------------------------------------------------------------------
# File-type registry
# ---------------------------------------------------------------------------

CATEGORIES = {
    "Image": {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp",
              ".heic", ".heif", ".bmp", ".gif"},
    "Audio": {".mp3", ".flac", ".ogg", ".oga", ".opus", ".wav",
              ".m4a", ".aac", ".wma", ".aiff", ".aif"},
    "Video": {".mp4", ".m4v", ".mov", ".mkv", ".avi", ".wmv", ".webm"},
    "Document": {".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"},
}

OOXML_EXTS = {".docx", ".xlsx", ".pptx"}

EXT_TO_CATEGORY = {}
for _cat, _exts in CATEGORIES.items():
    for _e in _exts:
        EXT_TO_CATEGORY[_e] = _cat


def category_of(path):
    return EXT_TO_CATEGORY.get(os.path.splitext(path)[1].lower())


def is_supported(path):
    return category_of(path) is not None


# ---------------------------------------------------------------------------
# Locating exiftool
# ---------------------------------------------------------------------------

_exiftool_path = None


def find_exiftool():
    """Locate the exiftool binary: bundled copy first, then PATH."""
    global _exiftool_path
    if _exiftool_path and os.path.isfile(_exiftool_path):
        return _exiftool_path

    candidates = []
    # PyInstaller onefile bundle
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, "exiftool.exe"))
    # Next to this script / the exe
    base = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(base, "exiftool.exe"))
    candidates.append(os.path.join(base, "exiftool"))
    # PATH
    for name in ("exiftool.exe", "exiftool"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    for c in candidates:
        if c and os.path.isfile(c):
            _exiftool_path = c
            return c
    return None


def exiftool_available():
    return find_exiftool() is not None or _perl_direct_argv() is not None


def _perl_direct_argv():
    """[perl.exe, exiftool.pl] if a bundled Strawberry Perl is present.

    The Windows ExifTool package ships a tiny launcher exe plus a Strawberry
    Perl in exiftool_files/. The launcher embeds the Perl DLL but configures
    @INC unreliably in some environments (notably PyInstaller onefile temp
    dirs), dying with "Can't locate strict.pm". Calling the bundled perl.exe
    directly runs the exact same exiftool.pl with a correctly resolved @INC,
    and also avoids the launcher's console/pause behavior.
    """
    search = []
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        search.append(sys._MEIPASS)
    search.append(os.path.dirname(os.path.abspath(__file__)))
    for base in search:
        perl = os.path.join(base, "exiftool_files", "perl.exe")
        script = os.path.join(base, "exiftool_files", "exiftool.pl")
        if os.path.isfile(perl) and os.path.isfile(script):
            return [perl, script]
    return None


def exiftool_argv():
    """Argv list used to invoke exiftool (perl-direct preferred)."""
    argv = _perl_direct_argv()
    if argv:
        return argv
    exe = find_exiftool()
    if not exe:
        raise RuntimeError("exiftool not found")
    return [exe]


def _popen_kwargs():
    # Keep console windows from flashing on Windows (frozen --windowed app
    # calling the console-mode perl.exe).
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return {"startupinfo": si,
                "creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _run_et(args, timeout=120):
    """Run exiftool with the given args; returns (returncode, stdout, stderr)."""
    cmd = exiftool_argv()
    # -charset filename=utf8 keeps non-ASCII paths working on Windows
    proc = subprocess.run(
        cmd + ["-charset", "filename=utf8"] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        **_popen_kwargs(),
    )
    return (proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# Reading metadata
# ---------------------------------------------------------------------------

def get_metadata(path):
    """Return the raw exiftool tag dict for one file (group-prefixed keys)."""
    rc, out, err = _run_et(["-j", "-G", "-a", path])
    if rc != 0:
        raise RuntimeError("exiftool read failed: %s" % (err.strip() or out.strip()))
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError("could not parse exiftool output: %s" % exc)
    if not data:
        return {}
    return data[0]


def scan_folder_metadata(folder, recursive=False):
    """Run one exiftool pass over a folder; returns {path: tagdict}."""
    args = ["-j", "-G", "-a"]
    if recursive:
        args.append("-r")
    args.append(folder)
    rc, out, err = _run_et(args, timeout=600)
    if rc != 0 and not out.strip():
        raise RuntimeError("exiftool scan failed: %s" % (err.strip() or "unknown error"))
    try:
        data = json.loads(out) if out.strip() else []
    except json.JSONDecodeError as exc:
        raise RuntimeError("could not parse exiftool output: %s" % exc)
    result = {}
    for entry in data:
        src = entry.get("SourceFile")
        if src:
            result[os.path.abspath(src)] = entry
    return result


def group_name(tag_key):
    """'EXIF:Make' -> 'EXIF'. Keys without a group go to 'File'."""
    if ":" in tag_key:
        return tag_key.split(":", 1)[0]
    return "File"


def parse_grouped(tagdict):
    """{group: [(tag, value), ...]} with groups and tags sorted."""
    groups = {}
    for key, value in tagdict.items():
        if key in ("SourceFile", "Directory", "FileName"):
            continue
        grp = group_name(key)
        tag = key.split(":", 1)[1] if ":" in key else key
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        else:
            value = str(value)
        groups.setdefault(grp, []).append((tag, value))
    for grp in groups:
        groups[grp].sort(key=lambda t: t[0].lower())
    return dict(sorted(groups.items()))


# Groups that are structural, derived, or deliberately preserved rather
# than removable personal metadata. Ignored by the tag count and the
# Clean / Has-metadata status (they are still shown in the viewer).
# Verified empirically against ExifTool 13.59's `-all=` leftovers.
IGNORED_GROUPS = frozenset({
    "File",        # filesystem + container structural tags (see COUNTED_FILE_TAGS)
    "ExifTool",    # exiftool's own version tag
    "Composite",   # computed on the fly (ImageSize, Megapixels, ...)
    "ICC_Profile", # color profile - deliberately preserved across scrubs
    "ICC",
    "JFIF",        # APP0 marker: version/density, no personal data
    "ZIP",         # zip container directory (names/sizes/CRCs), not metadata
})

# Groups with MIXED content: only the known-structural tags are ignored,
# everything else (Encoder, GPSCoordinates, Author, Comment, ...) counts.
IGNORED_TAGS = {
    # PNG: IHDR + cHRM chromaticities survive `-all=` (structural).
    # tEXt/zTXt/iTXt comments, gAMA, sRGB, pHYs, tIME ARE deleted -> counted.
    "PNG": {"BitDepth", "ColorType", "Compression", "Filter",
            "ImageHeight", "ImageWidth", "Interlace",
            "WhitePointX", "WhitePointY", "RedX", "RedY",
            "GreenX", "GreenY", "BlueX", "BlueY"},
    # GIF: descriptor + animation timing survive `-all=` (structural).
    # Comment extensions ARE deleted -> counted (via File:Comment).
    "GIF": {"BackgroundColor", "BitsPerPixel", "ColorResolutionDepth",
            "GIFVersion", "HasColorMap", "ImageHeight", "ImageWidth",
            "AnimationIterations", "Duration", "FrameCount",
            "TransparentColor"},
    "MPEG": {"AudioBitrate", "AudioLayer", "ChannelMode", "CopyrightFlag",
             "Emphasis", "IntensityStereo", "MPEGAudioVersion", "MSStereo",
             "OriginalMedia", "SampleRate"},
    # QuickTime mvhd/tkhd/mdhd box fields: required for playback,
    # `-all=` does not delete them.
    "QuickTime": {"AudioBitsPerSample", "AudioChannels", "AudioFormat",
                  "AudioSampleRate", "Balance", "BitDepth", "CompatibleBrands",
                  "CompressorID", "CreateDate", "CurrentTime", "Duration",
                  "GraphicsMode", "HandlerDescription", "HandlerType",
                  "ImageHeight", "ImageWidth", "MajorBrand", "MatrixStructure",
                  "MediaCreateDate", "MediaDataOffset", "MediaDataSize",
                  "MediaDuration", "MediaHeaderVersion", "MediaLanguageCode",
                  "MediaModifyDate", "MediaTimeScale", "MinorVersion",
                  "ModifyDate", "MovieHeaderVersion", "NextTrackID", "OpColor",
                  "PixelAspectRatio", "PosterTime", "PreferredRate",
                  "PreferredVolume", "PreviewDuration", "PreviewTime",
                  "SelectionDuration", "SelectionTime", "SourceImageHeight",
                  "SourceImageWidth", "TimeScale", "TrackCreateDate",
                  "TrackDuration", "TrackHeaderVersion", "TrackID",
                  "TrackLayer", "TrackModifyDate", "TrackVolume",
                  "VideoFrameRate", "XResolution", "YResolution"},
    # RIFF/WAV fmt-chunk fields (INFO tags like Artist ARE counted)
    "RIFF": {"AvgBytesPerSec", "BitsPerSample", "BlockAlign", "Encoding",
             "NumChannels", "SampleRate"},
    # PDF document structural (Info-dict Author/Title ARE counted)
    "PDF": {"Linearized", "PDFVersion", "PageCount"},
}

# File-group tags that ARE real removable metadata (exceptions to the
# File-group ignore above).
COUNTED_FILE_TAGS = frozenset({
    "Comment", "XPComment", "XPAuthor", "XPKeywords", "XPSubject", "XPTitle",
})


def _split_key(key):
    if ":" in key:
        g, t = key.split(":", 1)
        return g, t
    return "File", key


def count_metadata_tags(tagdict):
    """Count removable metadata tags, ignoring structural/derived ones."""
    n = 0
    for key in tagdict:
        grp, tag = _split_key(key)
        if grp == "File":
            if tag in COUNTED_FILE_TAGS:
                n += 1
            continue
        if grp in IGNORED_GROUPS:
            continue
        if tag in IGNORED_TAGS.get(grp, ()):
            continue
        n += 1
    return n


def has_gps(tagdict):
    for key in tagdict:
        grp, tag = _split_key(key)
        if grp == "GPS" and tag != "GPSVersionID":
            return True
        # GPS data surfaced under other groups (MP4, XMP, Composite)
        if tag in ("GPSCoordinates", "GPSPosition",
                   "GPSLatitude", "GPSLongitude"):
            return True
    return False


def has_thumbnail(tagdict):
    for key in tagdict:
        base = key.split(":", 1)[-1]
        if base in ("ThumbnailImage", "PreviewImage", "ThumbnailTIFF",
                    "MPImage2", "PreviewTIFF"):
            return True
    return False


def icc_description(tagdict):
    for key in ("ICC_Profile:ProfileDescription", "ICC:ProfileDescription"):
        if key in tagdict:
            return str(tagdict[key])
    return None


# ---------------------------------------------------------------------------
# ICC color-profile preservation
# ---------------------------------------------------------------------------

def extract_icc_profile(path):
    """Return raw ICC profile bytes, or None if the file has none."""
    cmd = exiftool_argv()
    proc = subprocess.run(
        cmd + ["-b", "-ICC_Profile", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        **_popen_kwargs(),
    )
    data = proc.stdout
    if proc.returncode == 0 and data and len(data) > 32:
        return data
    return None


def write_icc_profile(path, profile_bytes):
    """Write ICC profile bytes back into a file. Returns True on success."""
    fd, tmp = tempfile.mkstemp(suffix=".icc")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(profile_bytes)
        rc, out, err = _run_et(
            ["-overwrite_original", "-ICC_Profile<=%s" % tmp, path])
        return rc == 0
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# OOXML (docx/xlsx/pptx) extra sanitizing, pure stdlib
# ---------------------------------------------------------------------------

_CORE_NS = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}

_MINIMAL_CORE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
</cp:coreProperties>
"""

_MINIMAL_APP = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
</Properties>
"""

_EMPTY_COMMENTS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
</w:comments>
"""


def _is_rels(name):
    return name.endswith(".rels")


def _resolve_rel_target(rels_name, target):
    """Resolve a .rels Target against the rels file's location.

    e.g. rels "word/_rels/document.xml.rels" + target "customXml/item1.xml"
    -> "word/customXml/item1.xml". Handles ../ and ./ segments.
    """
    if rels_name == "_rels/.rels":
        base = ""
    elif "/_rels/" in rels_name:
        base = rels_name.rsplit("/_rels/", 1)[0] + "/"
    else:  # shouldn't happen, but be conservative
        base = os.path.dirname(rels_name).replace("\\", "/").rstrip("/") + "/"
    parts = []
    for seg in (base + target).replace("\\", "/").split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


def sanitize_ooxml(path):
    """Strip author/revision/comment/custom metadata from an OOXML package.

    Rewrites docProps/core.xml and docProps/app.xml with empty shells,
    drops docProps/custom.xml, empties comment parts, and prunes any
    .rels entries that pointed at removed parts so the package stays valid.
    Returns a list of human-readable notes about what was changed.
    """
    notes = []
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    removed_parts = set()

    try:
        zin = zipfile.ZipFile(path, "r")
        names = zin.namelist()

        # Decide what to drop / neutralize first
        drop = set()
        for n in names:
            if n == "docProps/custom.xml":
                drop.add(n)
            elif n.startswith("word/customXml/") or n.startswith("xl/customXml/") \
                    or n.startswith("ppt/customXml/"):
                drop.add(n)
        removed_parts.update(drop)

        # Collect .rels files so we can prune dangling references
        rels_names = [n for n in names if _is_rels(n)]
        rels_docs = {}
        for rn in rels_names:
            try:
                rels_docs[rn] = ET.fromstring(zin.read(rn))
            except ET.ParseError:
                rels_docs[rn] = None

        def rel_targets_removed(root, rels_name):
            if root is None:
                return False
            changed = False
            ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
            for rel in list(root.findall(ns + "Relationship")):
                tgt = rel.get("Target", "")
                if not tgt or tgt.startswith(("http://", "https://", "mailto:")):
                    continue  # external targets are never our removed parts
                resolved = _resolve_rel_target(rels_name, tgt)
                if resolved in removed_parts:
                    root.remove(rel)
                    changed = True
            return changed

        zout = zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED)
        try:
            for item in zin.infolist():
                n = item.filename
                if n in drop:
                    notes.append("removed %s" % n)
                    continue
                data = zin.read(n)
                if n == "docProps/core.xml":
                    data = _MINIMAL_CORE.encode("utf-8")
                    notes.append("cleared document core properties (author, revisions)")
                elif n == "docProps/app.xml":
                    data = _MINIMAL_APP.encode("utf-8")
                    notes.append("cleared extended properties")
                elif re.search(r"(word|xl|ppt)/comments\d*\.xml$", n):
                    data = _EMPTY_COMMENTS.encode("utf-8")
                    notes.append("emptied %s" % n)
                elif n in rels_docs and rels_docs[n] is not None:
                    if rel_targets_removed(rels_docs[n], n):
                        data = ET.tostring(rels_docs[n], encoding="utf-8",
                                           xml_declaration=True)
                        notes.append("pruned dangling relationships in %s" % n)
                zout.writestr(item, data)
        finally:
            zout.close()
        zin.close()

        shutil.move(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return notes


# ---------------------------------------------------------------------------
# Scrubbing
# ---------------------------------------------------------------------------

# Audio formats ExifTool 13.59 cannot write -> pure-Python strippers.
_STRIPPER_EXTS = {
    ".mp3": strip_id3_tags,
    ".aac": strip_id3_tags,   # raw ADTS sometimes carries a leading ID3v2
    ".flac": strip_flac_tags,
    ".ogg": strip_ogg_tags,
    ".oga": strip_ogg_tags,
    ".opus": strip_ogg_tags,
}


def _new_result(path):
    return {
        "path": path,
        "ok": False,
        "tags_before": 0,
        "tags_after": 0,
        "icc_preserved": False,
        "icc_description": None,
        "notes": [],
        "error": None,
    }


def _scrub_with_stripper(path, strip):
    """Scrub via a pure-Python byte-level stripper (MP3/FLAC/Ogg)."""
    result = _new_result(path)
    try:
        before = get_metadata(path)
        result["tags_before"] = count_metadata_tags(before)
        with open(path, "rb") as f:
            data = f.read()
        new_data, notes = strip(data)
        result["notes"].extend(notes)
        if new_data != data:
            if len(new_data) < 64:
                raise ValueError("stripper produced an implausibly small "
                                 "file; aborting to protect your data")
            tmp = path + ".metascrub-tmp"
            with open(tmp, "wb") as f:
                f.write(new_data)
            os.replace(tmp, path)  # atomic
            result["notes"].append("rewrote file (%d -> %d bytes)"
                                  % (len(data), len(new_data)))
        after = get_metadata(path)
        result["tags_after"] = count_metadata_tags(after)
        result["ok"] = True
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _scrub_ooxml(path):
    """Scrub Office Open XML via direct package sanitize.

    ExifTool cannot write OOXML at all, so the Python sanitizer does the
    whole job; afterwards we verify by re-reading.
    """
    result = _new_result(path)
    try:
        before = get_metadata(path)
        result["tags_before"] = count_metadata_tags(before)
        result["notes"].extend(sanitize_ooxml(path))
        result["notes"].append(
            "exiftool cannot write OOXML; used direct package sanitize")
        after = get_metadata(path)
        result["tags_after"] = count_metadata_tags(after)
        result["ok"] = True
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _scrub_exiftool(path):
    """Scrub via exiftool -all=, preserving the ICC profile."""
    result = _new_result(path)
    try:
        before = get_metadata(path)
        result["tags_before"] = count_metadata_tags(before)
        desc = icc_description(before)
        if desc:
            result["icc_description"] = desc

        # 1. Preserve the color profile before wiping.
        icc = extract_icc_profile(path)
        if icc:
            result["notes"].append(
                "saved ICC profile (%s, %d bytes)"
                % (desc or "unnamed", len(icc)))

        # 2. The big wipe.
        rc, out, err = _run_et(["-all=", "-overwrite_original", path])
        updated = re.search(r"1 image files? updated", out)
        if rc != 0 and not updated:
            msg = (err.strip() or out.strip()).splitlines()
            result["error"] = "exiftool: %s" % (msg[0] if msg else "unknown error")
            return result

        # 3. Put the color profile back.
        if icc:
            if write_icc_profile(path, icc):
                result["icc_preserved"] = True
                result["notes"].append("restored ICC profile after scrub")
            else:
                result["notes"].append(
                    "WARNING: could not restore ICC profile")

        # 4. Verify.
        after = get_metadata(path)
        result["tags_after"] = count_metadata_tags(after)
        result["ok"] = True
    except Exception as exc:
        result["error"] = str(exc)
    return result


def scrub_file(path):
    """Scrub all removable metadata from one file.

    Routes to the right engine per format: exiftool -all= for formats it
    can write, pure-Python strippers for MP3/FLAC/Ogg and OOXML.
    Returns a result dict.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in OOXML_EXTS:
        return _scrub_ooxml(path)
    strip = _STRIPPER_EXTS.get(ext)
    if strip:
        return _scrub_with_stripper(path, strip)
    return _scrub_exiftool(path)


def list_supported_files(folder, recursive=False):
    """Return sorted list of supported file paths under folder."""
    found = []
    if recursive:
        for root, _dirs, files in os.walk(folder):
            for f in files:
                p = os.path.join(root, f)
                if is_supported(p):
                    found.append(p)
    else:
        try:
            entries = os.listdir(folder)
        except OSError:
            return []
        for f in entries:
            p = os.path.join(folder, f)
            if os.path.isfile(p) and is_supported(p):
                found.append(p)
    return sorted(found)
