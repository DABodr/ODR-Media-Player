from dataclasses import dataclass, field
import random

from .playlist_state import move_item, remove_item


@dataclass
class Track:
    path: str
    artist: str = ""
    title: str = ""
    album: str = ""
    duration: str = "?"
    manual_metadata: bool = False
    source_pid: int = 0
    source_app_name: str = ""


@dataclass
class PlaylistModel:
    tracks: list[Track] = field(default_factory=list)
    current_idx: int = -1
    paused: bool = False
    manual_skip: bool = False

    def __len__(self):
        return len(self.tracks)

    def __bool__(self):
        return bool(self.tracks)

    def __iter__(self):
        return iter(self.tracks)

    def __getitem__(self, index):
        return self.tracks[index]

    def append(self, track):
        self.tracks.append(track)

    def clear(self):
        self.tracks.clear()
        self.current_idx = -1
        self.paused = False

    def current_track(self):
        if 0 <= self.current_idx < len(self.tracks):
            return self.tracks[self.current_idx]
        return None

    def ensure_current(self):
        if self.current_idx < 0 and self.tracks:
            self.current_idx = 0
        return self.current_idx

    def set_current(self, index):
        if 0 <= index < len(self.tracks):
            self.current_idx = index
            return self.tracks[index]
        return None

    def previous_index(self):
        if not self.tracks:
            return None
        if self.current_idx > 0:
            return self.current_idx - 1
        return 0

    def next_index(self, shuffle_enabled, repeat_enabled):
        if not self.tracks:
            return None
        if shuffle_enabled:
            return random.randint(0, len(self.tracks) - 1)

        next_index = self.current_idx + 1
        if next_index >= len(self.tracks):
            if repeat_enabled:
                return 0
            return None
        return next_index

    def move(self, source_idx, target_idx):
        self.current_idx = move_item(self.tracks, self.current_idx, source_idx, target_idx)
        return self.current_idx

    def remove_at(self, index):
        self.current_idx = remove_item(self.tracks, self.current_idx, index)
        return self.current_idx

    def stop(self):
        self.current_idx = -1
        self.paused = False

    def paths(self):
        return [track.path for track in self.tracks]
