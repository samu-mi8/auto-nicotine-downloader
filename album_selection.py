"""Logic for choosing the best folder among the search results.

It does not depend on Nicotine+, so it can be tested on its own.
"""

import re
import unicodedata
from collections import Counter

# Soulseek file attribute keys (pynicotine.slskmessages.FileAttribute)
ATTR_BITRATE = 0
ATTR_VBR = 2
ATTR_SAMPLE_RATE = 4
ATTR_BIT_DEPTH = 5

FORMAT_EXTENSIONS = {"FLAC": "flac", "MP3": "mp3"}
AUDIO_EXTENSIONS = {
    "flac", "mp3", "m4a", "aac", "alac", "ogg", "opus", "wav", "aif", "aiff", "ape", "wv", "wma", "dsf", "dff"
}

# Letters that NFKD does not decompose ("Ágætis" -> "agaetis")
TRANSLITERATIONS = str.maketrans({"æ": "ae", "ø": "o", "œ": "oe", "ß": "ss", "ð": "d", "þ": "th", "ł": "l", "đ": "d"})

# Subfolders like "CD1", "Disc 2", "Disco 1 - Bonus"
DISC_FOLDER_RE = re.compile(r"^(cd|disc|disk|disco)[\s._-]*\d+\b", re.IGNORECASE)


def normalize_words(text):
    """Lowercase words, without accents or punctuation ("Sgt. Pepper's" -> ["sgt", "peppers"])."""

    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower().translate(TRANSLITERATIONS).replace("'", "").replace("’", "")
    return re.sub(r"[\W_]+", " ", text).split()


def parse_query(query):
    """'Artist - Album' -> (artist, album, folder name)."""

    query = " ".join(query.split())

    for separator in (" - ", " – ", " — "):
        if separator in query:
            artist, _sep, album = query.partition(separator)
            artist, album = artist.strip(), album.strip()
            return artist, album, f"{artist} - {album}"

    return "", query, query


def file_extension(path):
    basename = path.rpartition("\\")[2]
    return basename.rpartition(".")[2].lower() if "." in basename else ""


def most_common(values):
    values = [value for value in values if value]
    return Counter(values).most_common(1)[0][0] if values else None


class Candidate:
    """A folder (album) shared by a user."""

    def __init__(self, username, folder_path, free_slots, speed, queue):

        self.username = username
        self.folder_path = folder_path
        self.free_slots = bool(free_slots)
        self.speed = speed or 0
        self.queue = queue or 0

        # virtual_path -> (size, attrs, relative subfolder, e.g. "" or "CD1")
        self.files = {}
        self.source_folders = set()

        self.format = None
        self.num_tracks = 0
        self.mixed_formats = False
        self.bit_depth = None
        self.sample_rate = None
        self.bitrate = None
        self.is_hires = False
        self.complete = True
        self.name_match = False

    def add_file(self, virtual_path, size, attrs, subfolder):
        self.files[virtual_path] = (size, attrs or {}, subfolder)
        self.source_folders.add(virtual_path.rpartition("\\")[0])

    def analyze(self):

        counts = Counter(file_extension(path) for path in self.files)
        num_flac = counts.get("flac", 0)
        num_mp3 = counts.get("mp3", 0)

        if not num_flac and not num_mp3:
            return

        self.format = "FLAC" if num_flac >= num_mp3 else "MP3"
        self.num_tracks = max(num_flac, num_mp3)
        self.mixed_formats = bool(num_flac and num_mp3)

        extension = FORMAT_EXTENSIONS[self.format]
        attrs_list = [attrs for path, (_size, attrs, _sub) in self.files.items() if file_extension(path) == extension]

        self.bit_depth = most_common(attrs.get(ATTR_BIT_DEPTH) for attrs in attrs_list)
        self.sample_rate = most_common(attrs.get(ATTR_SAMPLE_RATE) for attrs in attrs_list)

        bitrates = sorted(attrs[ATTR_BITRATE] for attrs in attrs_list if attrs.get(ATTR_BITRATE))
        self.bitrate = bitrates[len(bitrates) // 2] if bitrates else None

        self.is_hires = self.format == "FLAC" and (
            (self.bit_depth or 16) > 16 or (self.sample_rate or 44100) > 48000
        )

    def quality_bucket(self, prefer_hires):
        """Higher is better. Hi-res FLAC > FLAC > MP3 320 > MP3 V0/256 > other MP3."""

        if self.format == "FLAC":
            return 5 if (self.is_hires and prefer_hires) else 4

        if self.bitrate is None:
            return 0

        if self.bitrate >= 315:
            return 3

        if self.bitrate >= 220:
            return 2

        return 1

    def fine_quality(self):
        if self.format == "FLAC":
            return (self.bit_depth or 16) * 1_000_000 + (self.sample_rate or 44100)

        return self.bitrate or 0

    def quality_label(self):

        if self.format == "FLAC":
            if self.bit_depth or self.sample_rate:
                depth = self.bit_depth or 16
                rate = (self.sample_rate or 44100) / 1000
                return f"FLAC {depth}bit/{rate:g}kHz"

            return "FLAC"

        if self.bitrate:
            return f"MP3 {self.bitrate}kbps"

        return "MP3"

    def availability_label(self):
        return "free slot" if self.free_slots else f"queued: {self.queue}"

    def describe(self):
        return (f"[{self.quality_label()}] {self.num_tracks} tracks · {self.username} "
                f"({self.availability_label()}) · {self.folder_path}")


def build_candidates(responses):
    """responses: list of (username, file_list, free_slots, speed, queue).

    file_list holds (code, virtual_path, size, ext, attrs) tuples, as in FileSearchResponse.
    Files inside subfolders like "CD1" are grouped under the album folder.
    """

    candidates = {}

    for username, file_list, free_slots, speed, queue in responses:
        for _code, virtual_path, size, _ext, attrs, *_unused in file_list or []:
            folder_path, _sep, _basename = virtual_path.rpartition("\\")

            if not folder_path:
                continue

            parent_path, _sep, folder_name = folder_path.rpartition("\\")
            subfolder = ""

            if parent_path and DISC_FOLDER_RE.match(folder_name):
                folder_path, subfolder = parent_path, folder_name

            key = (username, folder_path)
            candidate = candidates.get(key)

            if candidate is None:
                candidate = candidates[key] = Candidate(username, folder_path, free_slots, speed, queue)

            candidate.add_file(virtual_path, size, attrs, subfolder)

    return list(candidates.values())


def rank_candidates(responses, album, min_mp3_bitrate=192, prefer_hires=True):
    """Returns the suitable folders, from best to worst."""

    album_words = set(normalize_words(album))
    candidates = []

    for candidate in build_candidates(responses):
        candidate.analyze()

        if candidate.format is None:
            continue

        if (candidate.format == "MP3" and candidate.bitrate is not None
                and candidate.bitrate < min_mp3_bitrate):
            continue

        # The album title must appear in the folder path, not just in the file names
        candidate.name_match = album_words.issubset(normalize_words(candidate.folder_path))

        if candidate.name_match:
            candidates.append(candidate)

    if not candidates:
        return []

    # "Expected" track count: the most common among the folders found (the highest on ties)
    track_counts = Counter(c.num_tracks for c in candidates if c.num_tracks >= 2)

    if track_counts:
        expected = max(track_counts.items(), key=lambda item: (item[1], item[0]))[0]

        for candidate in candidates:
            candidate.complete = candidate.num_tracks >= expected

    candidates.sort(
        key=lambda c: (
            c.complete,
            c.quality_bucket(prefer_hires),
            c.free_slots,
            not c.mixed_formats,
            -c.queue,
            c.speed,
            c.fine_quality(),
            c.num_tracks
        ),
        reverse=True
    )
    return candidates


def should_download_file(virtual_path, chosen_format):
    """Download cover art, cue, log etc., but not audio files in a format other than the chosen one."""

    extension = file_extension(virtual_path)
    return extension not in AUDIO_EXTENSIONS or extension == FORMAT_EXTENSIONS[chosen_format]
