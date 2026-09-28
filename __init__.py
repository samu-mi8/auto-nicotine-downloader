"""Album Downloader: cerca un album, sceglie la cartella di qualità più alta e la scarica
in "<cartella musica>\\Artista - Album".

Uso:
- nella barra di ricerca:   album: Artista - Album
- in una chat:              /album Artista - Album
- altre opzioni trovate:    /album-list   e   /album-pick <numero>
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
                "description": "Cartella della musica (qui viene creata la cartella \"Artista - Album\")",
                "type": "file",
                "chooser": "folder"
            },
            "search_prefix": {
                "description": "Prefisso da usare nella barra di ricerca (es. album: Artista - Album)",
                "type": "string"
            },
            "wait_seconds": {
                "description": "Secondi di attesa dei risultati prima di scegliere",
                "type": "integer", "minimum": 5, "maximum": 120
            },
            "min_mp3_bitrate": {
                "description": "Bitrate minimo accettato per gli MP3 (se non ci sono FLAC)",
                "type": "integer", "minimum": 0, "maximum": 320
            },
            "prefer_hires": {
                "description": "Preferisci FLAC hi-res (24bit / >48kHz) ai FLAC normali",
                "type": "bool"
            },
            "auto_download": {
                "description": "Scarica subito la cartella migliore (altrimenti mostra solo la lista)",
                "type": "bool"
            }
        }
        self.commands = {
            "album": {
                "callback": self.album_command,
                "description": "Cerca un album e scarica la versione di qualità più alta",
                "parameters": ["<Artista - Album..>"]
            },
            "album-list": {
                "callback": self.album_list_command,
                "description": "Mostra le cartelle trovate per l'ultimo album cercato"
            },
            "album-pick": {
                "callback": self.album_pick_command,
                "description": "Scarica la cartella numero N dell'ultima ricerca",
                "parameters": ["<numero>"]
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

    # Avvio ricerca #

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

        self._notify(job, f"Cerco \"{query}\"… scelgo la versione migliore tra {wait_seconds} secondi.")

    def _file_search_response(self, msg):

        job = self.jobs.get(msg.token)

        if job is None or job.finished or not msg.list:
            return

        job.responses.append((msg.username, msg.list, msg.freeulslots, msg.ulspeed, msg.inqueue))

    # Scelta #

    def _finish_search(self, token):

        # Chiamato dallo scheduler di Nicotine+: un'eccezione qui chiuderebbe l'intero programma
        try:
            self._choose_candidate(token)

        except Exception as error:
            self.log(f"Errore durante la scelta dell'album: {error!r}")

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
            self._notify(job, f"Nessuna cartella FLAC/MP3 adatta trovata per \"{job.query}\" "
                              f"({len(job.responses)} utenti hanno risposto).", popup=True)
            return

        if self.settings["auto_download"]:
            self._download(job, 0)
        else:
            self._notify(job, f"Trovate {len(job.candidates)} cartelle per \"{job.query}\". "
                              "Scegli con /album-pick <numero>.", popup=True)

        self._show_candidates(job)

    def _show_candidates(self, job):

        lines = [f"Cartelle trovate per \"{job.query}\" (la prima è la migliore):"]

        for number, candidate in enumerate(job.candidates[:MAX_LISTED_CANDIDATES], start=1):
            lines.append(f"  {number}. {candidate.describe()}")

        if len(job.candidates) > 1:
            lines.append("Per scaricarne un'altra: /album-pick <numero>")

        self._notify(job, "\n".join(lines))

    # Download #

    def _download(self, job, index, force=False):

        candidate = job.candidates[index]
        music_folder = os.path.normpath(os.path.expandvars(self.settings["music_folder"]))
        destination = os.path.join(music_folder, clean_file(job.folder_name))

        if not force and os.path.isdir(destination) and os.listdir(destination):
            self._notify(job, f"La cartella \"{destination}\" esiste già e non è vuota, non scarico niente. "
                              f"Per scaricare comunque: /album-pick {index + 1}", popup=True)
            return

        # File già visti nei risultati di ricerca
        for virtual_path, (size, attrs, subfolder) in candidate.files.items():
            if album_selection.should_download_file(virtual_path, candidate.format):
                self.core.downloads.enqueue_download(
                    candidate.username, virtual_path, folder_path=os.path.join(destination, subfolder),
                    size=size, file_attributes=attrs)

        # Chiedi il contenuto completo delle cartelle (copertina, tracce non comparse nella ricerca…)
        for folder_path in {candidate.folder_path} | candidate.source_folders:
            key = (candidate.username, folder_path)
            self.folder_requests[key] = (candidate, destination)
            self.core.send_message_to_peer(
                candidate.username, FolderContentsRequest(folder_path, random.randint(1, 2**31 - 1)))
            events.schedule(
                delay=FOLDER_REQUEST_TIMEOUT, callback=self.folder_requests.pop, callback_args=(key, None))

        self._notify(job, f"Download avviato: {candidate.describe()} → {destination}", popup=True)

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

    # Comandi #

    def album_list_command(self, _args, **_unused):

        if self.last_job is None or not self.last_job.finished:
            self.output("Nessuna ricerca album completata finora.")
            return

        if not self.last_job.candidates:
            self.output(f"Nessuna cartella adatta trovata per \"{self.last_job.query}\".")
            return

        self._show_candidates(self.last_job)

    def album_pick_command(self, args, **_unused):

        job = self.last_job

        if job is None or not job.finished or not job.candidates:
            self.output("Nessuna ricerca album con risultati da cui scegliere.")
            return False

        try:
            index = int(args.split()[0]) - 1
        except ValueError:
            index = -1

        if not 0 <= index < len(job.candidates):
            self.output(f"Numero non valido: scegli tra 1 e {len(job.candidates)}.")
            return False

        # Il comando arriva da questa chat: le risposte vanno qui
        job.source = self.parent.command_source
        self._download(job, index, force=True)
        return True

    # Messaggi #

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
