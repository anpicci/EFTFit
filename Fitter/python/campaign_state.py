"""Append-only local campaign transactions; committed history is the pointer.

An event is visible only after its exclusive hash-bearing commit marker exists.
An interrupted event remains visible as incomplete and blocks further writes.
There is no replaceable active-generation pointer or automatic rollback.
"""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import uuid

from .execution_provenance import read_json, write_json_exclusive
from .campaign_spec import digest


def new_id():
    return uuid.uuid4().hex


def checked_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("campaign/generation/attempt ID must be 32 lowercase hex characters")
    return value


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish(path, value):
    write_json_exclusive(path, value)
    sync_directory(Path(path).parent)


class campaign_store:
    def __init__(self, root, campaign_id):
        self.path = Path(root).resolve() / checked_id(campaign_id)
        self.campaign_id = campaign_id

    @contextmanager
    def locked(self):
        # Lock the immutable identity file: status/dry-run never create locks.
        with (self.path / "campaign.json").open("rb") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def read(self):
        result = {"campaign_id": self.campaign_id, "events": [], "incomplete": [], "revision": 0}
        try:
            campaign = read_json(self.path / "campaign.json")
            if campaign["schema_version"] != 1 or campaign["campaign_id"] != self.campaign_id or digest(campaign["effective_spec"]) != campaign["spec_digest"]:
                raise ValueError("invalid campaign identity/spec digest")
            result["campaign"] = campaign
        except (OSError, ValueError, KeyError, TypeError) as error:
            result["incomplete"].append("campaign record: " + str(error))
            return result
        previous = None
        # Snapshot marker names first; concurrent appends can only add history.
        markers = sorted(self.path.glob("event_*.commit.json"))
        for index, marker in enumerate(markers, 1):
            try:
                if marker.name != "event_{:08d}.commit.json".format(index):
                    raise ValueError("noncontiguous committed history")
                receipt = read_json(marker)
                event = read_json(self.path / "event_{:08d}.json".format(index))
                if receipt != {"event_digest": digest(event)} or event["previous"] != previous or event["revision"] != index or event["schema_version"] != 1 or event["campaign_id"] != self.campaign_id:
                    raise ValueError("invalid transaction identity/hash chain")
                result["events"].append(event)
                result["revision"] = index
                previous = digest(event)
            except (OSError, ValueError, KeyError, TypeError) as error:
                result["incomplete"].append(marker.name + ": " + str(error))
                break
        known = {"event_{:08d}.json".format(index) for index in range(1, result["revision"] + 1)}
        for path in sorted(self.path.glob("event_*.json")):
            if ".commit." not in path.name and path.name not in known:
                result["incomplete"].append(path.name + ": uncommitted transaction")
        for path in sorted(self.path.glob(".event_*.tmp")):
            result["incomplete"].append(path.name + ": interrupted exclusive publication")
        if not result["events"]:
            result["incomplete"].append("initial generation not committed")
        return result

    def append(self, snapshot, kind, payload):
        if snapshot["incomplete"]:
            raise ValueError("incomplete transaction: inspect status; explicit recovery required")
        revision = snapshot["revision"] + 1
        previous = digest(snapshot["events"][-1]) if snapshot["events"] else None
        event = {"schema_version": 1, "campaign_id": self.campaign_id, "revision": revision,
                 "previous": previous, "kind": kind, "payload": payload}
        publish(self.path / "event_{:08d}.json".format(revision), event)
        publish(self.path / "event_{:08d}.commit.json".format(revision), {"event_digest": digest(event)})
        reopened = self.read()
        if reopened["incomplete"] or reopened["events"][-1] != event:
            raise RuntimeError("transaction readback failed")
        return reopened

    def create(self, spec, source_yaml, points, parent=None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.mkdir()  # exclusive identity/namespace reservation
        sync_directory(self.path.parent)
        publish(self.path / "campaign.json", {"schema_version": 1, "campaign_id": self.campaign_id,
                "effective_spec": spec, "spec_digest": digest(spec), "source_yaml": source_yaml,
                "parent_campaign_id": parent})
        with self.locked():
            return self.append({"revision": 0, "events": [], "incomplete": []}, "generation",
                               {"generation_id": new_id(), "previous_generation_id": None, "points": points})
