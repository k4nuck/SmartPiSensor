import os
import subprocess

import pytest

import ecobee_store

# Pairing keys can control the thermostat and a snapshot commit from a live Pi checkout has happened before (dd91e9b),
# so every place the code writes keys must be git-ignored. Kept out of test_ecobee.py, which skips without aiohomekit.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _ignored(relative):
	return subprocess.run(["git", "check-ignore", "--no-index", "-q", relative], cwd=REPO).returncode == 0

@pytest.mark.parametrize("path", [
	"ecobee/pairings.json",
	"ecobee-pairing-ecobee-living-room.recovery.json",
	"fake_thermostat.state",
])
def test_key_files_are_git_ignored(path):
	assert _ignored(path), "%s is not covered by .gitignore" % path

def test_default_data_dir_is_git_ignored():
	pairings = ecobee_store.pairings_path({"log_path": os.path.join(REPO, "smartsensor.log")})
	assert _ignored(os.path.relpath(pairings, REPO))
