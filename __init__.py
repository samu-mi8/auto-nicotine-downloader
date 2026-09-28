"""Album Downloader: searches for an album, picks the highest quality folder and downloads it
to "<music folder>\\Artist - Album".

Usage:
- in the search bar:        album: Artist - Album
- in a chat:                /album Artist - Album
- other folders found:      /album-list   and   /album-pick <number>
"""

import os
import random

from pynicotine.events import events
from pynicotine.pluginsystem import BasePlugin
from pynicotine.slskmessages import FolderContentsRequest
from pynicotine.utils import clean_file

import album_selection

MAX_LISTED_CANDIDATES = 10
FOLDER_REQUEST_TIMEOUT = 180


class AlbumJob:

    def __init__(self, token, query, source):

        self.token = token
        self.query = query
        self.artist, self.album, self.folder_name = album_selection.parse_query(query)
        self.source = source
        self.responses = []
        self.candidates = []
        self.timer_id = None
        self.finished = False


class Plugin(BasePlugin):

    def __init__(self, *args, **kwargs):

        super().__init__(*args, **kwargs)

        self.settings = {
            "music_folder": r"C:\Users\samue\OneDrive\Musica\Music",
            "search_prefix": "album:",
            "wait_seconds": 20,
            "min_mp3_bitrate": 192,
            "prefer_hires": True,
            "auto_download": True
        }
        self.metasettings = {
            "music_folder": {
                "description": "Music folder (the \"Artist - Album\" folder is created here)",
                "type": "file",
                "chooser": "folder"
            },
            "search_prefix": {
                "description": "Prefix to use in the search bar (e.g. album: Artist - Album)",
                "type": "string"
            },
            "wait_seconds": {
                "description": "Seconds to wait for results before choosing",
                "type": "integer", "minimum": 5, "maximum": 120
            },
            "min_mp3_bitrate": {
                "description": "Minimum accepted MP3 bitrate (when no FLAC is available)",
                "type": "integer", "minimum": 0, "maximum": 320
            },
            "prefer_hires": {
                "description": "Prefer hi-res FLAC (24bit / >48kHz) over regular FLAC",
                "type": "bool"
            },
            "auto_download": {
                "description": "Download the best folder right away (otherwise only show the list)",
                "type": "bool"
            }
        }
        self.commands = {
            "album": {
                "callback": self.album_command,
                "description": "Search for an album and download the highest quality version",
                "parameters": ["<Artist - Album..>"]
            },
            "album-list": {
                "callback": self.album_list_command,
                "description": "Show the folders found for the last album search"
            },
            "album-pick": {
                "callback": self.album_pick_command,
                "description": "Download folder number N from the last search",
                "parameters": ["<number>"]
            }
        }

        self.jobs = {}
        self.last_job = None
        self.pending_queries = []
        self.folder_requests = {}

        self._event_callbacks = (
            ("add-search", self._add_search),
            ("file-search-response", self._file_search_response),
            ("folder-contents-response", self._folder_contents_response)
        )

    def loaded_notification(self):
        for event_name, callback in self._event_callbacks:
            events.connect(event_name, callback)

    def disable(self):

        for event_name, callback in self._event_callbacks:
            events.disconnect(event_name, callback)

        for job in self.jobs.values():
            if job.timer_id is not None:
                events.cancel_scheduled(job.timer_id)

        self.jobs.clear()
        self.folder_requests.clear()

    # Starting a search #

    def outgoing_global_search_event(self, text):

        prefix = self.settings["search_prefix"].strip()

        if not prefix or not text.lower().startswith(prefix.lower()):
            return None

        query = text[len(prefix):].strip()

        if not query:
            return None

        self.pending_queries.append((query, None))
        return (query,)

    def album_command(self, args, room=None, user=None):

        query = args.strip()
        source = ("chatroom", room) if room is not None else ("private_chat", user) if user is not None else None

        self.pending_queries.append((query, source))
        self.core.search.do_search(query, "global")

    def _add_search(self, token, search, *_args):

        for index, (query, source) in enumerate(self.pending_queries):
            if search.mode == "global" and search.term.strip() == query:
                del self.pending_queries[index]
                self._start_job(token, query, source)
                return

    def _start_job(self, token, query, source):

        job = AlbumJob(token, query, source)
        wait_seconds = max(5, int(self.settings["wait_seconds"]))

        job.timer_id = events.schedule(delay=wait_seconds, callback=self._finish_search, callback_args=(token,))
        self.jobs[token] = self.last_job = job

        self._notify(job, f"Searching for \"{query}\"… picking the best version in {wait_seconds} seconds.")

    def _file_search_response(self, msg):

        job = self.jobs.get(msg.token)

        if job is None or job.finished or not msg.list:
            return

        job.responses.append((msg.username, msg.list, msg.freeulslots, msg.ulspeed, msg.inqueue))

    # Choosing #

    def _finish_search(self, token):

        # Called by the Nicotine+ scheduler: an exception here would close the whole program
        try:
            self._choose_candidate(token)

        except Exception as error:
            self.log(f"Error while choosing the album: {error!r}")

    def _choose_candidate(self, token):

        job = self.jobs.pop(token, None)

        if job is None:
            return

        job.finished = True
        job.timer_id = None
        job.candidates = album_selection.rank_candidates(
            job.responses, job.album,
            min_mp3_bitrate=int(self.settings["min_mp3_bitrate"]),
            prefer_hires=bool(self.settings["prefer_hires"])
        )

        if not job.candidates:
            self._notify(job, f"No suitable FLAC/MP3 folder found for \"{job.query}\" "
                              f"({len(job.responses)} users responded).", popup=True)
            return

        if self.settings["auto_download"]:
            self._download(job, 0)
        else:
            self._notify(job, f"Found {len(job.candidates)} folders for \"{job.query}\". "
                              "Choose one with /album-pick <number>.", popup=True)

        self._show_candidates(job)

    def _show_candidates(self, job):

        lines = [f"Folders found for \"{job.query}\" (the first one is the best):"]

        for number, candidate in enumerate(job.candidates[:MAX_LISTED_CANDIDATES], start=1):
            lines.append(f"  {number}. {candidate.describe()}")

        if len(job.candidates) > 1:
            lines.append("To download a different one: /album-pick <number>")

        self._notify(job, "\n".join(lines))

    # Download #

    def _download(self, job, index, force=False):

        candidate = job.candidates[index]
        music_folder = os.path.normpath(os.path.expandvars(self.settings["music_folder"]))
        destination = os.path.join(music_folder, clean_file(job.folder_name))

        if not force and os.path.isdir(destination) and os.listdir(destination):
            self._notify(job, f"The folder \"{destination}\" already exists and is not empty, nothing downloaded. "
                              f"To download anyway: /album-pick {index + 1}", popup=True)
            return

        # Files already seen in the search results
        for virtual_path, (size, attrs, subfolder) in candidate.files.items():
            if album_selection.should_download_file(virtual_path, candidate.format):
                self.core.downloads.enqueue_download(
                    candidate.username, virtual_path, folder_path=os.path.join(destination, subfolder),
                    size=size, file_attributes=attrs)

        # Ask for the full folder contents (cover art, tracks missing from the search results…)
        for folder_path in {candidate.folder_path} | candidate.source_folders:
            key = (candidate.username, folder_path)
            self.folder_requests[key] = (candidate, destination)
            self.core.send_message_to_peer(
                candidate.username, FolderContentsRequest(folder_path, random.randint(1, 2**31 - 1)))
            events.schedule(
                delay=FOLDER_REQUEST_TIMEOUT, callback=self.folder_requests.pop, callback_args=(key, None))

        self._notify(job, f"Download started: {candidate.describe()} → {destination}", popup=True)

    def _folder_contents_response(self, msg):

        request = self.folder_requests.pop((msg.username, msg.dir), None)

        if request is None or not msg.list:
            return

        candidate, destination = request
        album_folder = candidate.folder_path

        for folder_path, files in msg.list.items():
            if folder_path != album_folder and not folder_path.startswith(album_folder + "\\"):
                continue

            relative_parts = [clean_file(part) for part in folder_path[len(album_folder):].split("\\") if part]
            folder_destination = os.path.join(destination, *relative_parts)

            for _code, basename, size, _ext, attrs, *_unused in files:
                virtual_path = folder_path.rstrip("\\") + "\\" + basename

                if album_selection.should_download_file(virtual_path, candidate.format):
                    self.core.downloads.enqueue_download(
                        candidate.username, virtual_path, folder_path=folder_destination,
                        size=size, file_attributes=attrs)

    # Commands #

    def album_list_command(self, _args, **_unused):

        if self.last_job is None or not self.last_job.finished:
            self.output("No album search completed yet.")
            return

        if not self.last_job.candidates:
            self.output(f"No suitable folder found for \"{self.last_job.query}\".")
            return

        self._show_candidates(self.last_job)

    def album_pick_command(self, args, **_unused):

        job = self.last_job

        if job is None or not job.finished or not job.candidates:
            self.output("No album search with results to choose from.")
            return False

        try:
            index = int(args.split()[0]) - 1
        except ValueError:
            index = -1

        if not 0 <= index < len(job.candidates):
            self.output(f"Invalid number: choose between 1 and {len(job.candidates)}.")
            return False

        # The command comes from this chat: replies go here
        job.source = self.parent.command_source
        self._download(job, index, force=True)
        return True

    # Messages #

    def _notify(self, job, text, popup=False):

        self.log(text)

        if job.source is not None:
            interface, target = job.source

            if interface == "chatroom":
                self.echo_public(target, text, message_type="command")
            elif interface == "private_chat":
                self.echo_private(target, text, message_type="command")

        if popup:
            self.core.notifications.show_download_notification(text, title="Album Downloader")
