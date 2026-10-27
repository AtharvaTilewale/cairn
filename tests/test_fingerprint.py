"""Tests for file fingerprints and the content-addressed object store."""

import hashlib
import os
import time

import pytest

from cairnlab.capture.fingerprint import FAST, FULL, fingerprint
from cairnlab.provenance.objects import has_object, object_path


def test_small_file_full_hash_and_stored(tmp_path):
    f = tmp_path / "rmsd.xvg"
    f.write_bytes(b"0.0 0.1\n1.0 0.2\n")
    objects = tmp_path / "objects"
    fp = fingerprint(f, objects_dir=objects)
    assert fp.method == FULL
    assert fp.sha256 == hashlib.sha256(f.read_bytes()).hexdigest()
    assert fp.size == f.stat().st_size
    assert fp.stored
    assert object_path(objects, fp.sha256).read_bytes() == f.read_bytes()
    assert has_object(objects, fp.sha256)


def test_hash_streams_beyond_one_chunk(tmp_path):
    f = tmp_path / "big.bin"
    data = os.urandom(3 * 1024 * 1024 + 17)
    f.write_bytes(data)
    assert fingerprint(f).sha256 == hashlib.sha256(data).hexdigest()


def test_without_object_store_nothing_is_copied(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    fp = fingerprint(f)
    assert fp.method == FULL and not fp.stored


def test_existing_object_is_not_rewritten(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("same")
    objects = tmp_path / "objects"
    sha = fingerprint(f, objects_dir=objects).sha256
    obj = object_path(objects, sha)
    before = obj.stat().st_mtime_ns
    time.sleep(0.01)
    fingerprint(f, objects_dir=objects)
    assert obj.stat().st_mtime_ns == before


def test_object_files_are_read_only(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("keep me")
    objects = tmp_path / "objects"
    obj = object_path(objects, fingerprint(f, objects_dir=objects).sha256)
    assert not obj.stat().st_mode & 0o222


def test_large_file_fast_fingerprint_not_stored(tmp_path):
    f = tmp_path / "traj.xtc"
    with f.open("wb") as fh:
        fh.write(b"A" * 1024 * 1024)
        fh.seek(5 * 1024 * 1024)
        fh.write(b"Z" * 1024 * 1024)
    objects = tmp_path / "objects"
    fp = fingerprint(f, objects_dir=objects, large_bytes=2 * 1024 * 1024)
    assert fp.method == FAST
    assert not fp.stored
    assert not objects.exists() or not any(objects.rglob("*"))
    # Same bytes, same mtime -> same fingerprint; a change in the tail changes it.
    assert fingerprint(f, large_bytes=2 * 1024 * 1024).sha256 == fp.sha256
    st = f.stat()
    with f.open("r+b") as fh:
        fh.seek(-1, os.SEEK_END)
        fh.write(b"Y")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert fingerprint(f, large_bytes=2 * 1024 * 1024).sha256 != fp.sha256


def test_fast_fingerprint_is_fast_on_a_sparse_huge_file(tmp_path):
    f = tmp_path / "huge.trr"
    with f.open("wb") as fh:
        fh.truncate(20 * 1024**3)  # 20 GB sparse file
    t0 = time.perf_counter()
    fp = fingerprint(f)
    assert fp.method == FAST and fp.size == 20 * 1024**3
    assert time.perf_counter() - t0 < 1.0


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        fingerprint(tmp_path / "nope")
