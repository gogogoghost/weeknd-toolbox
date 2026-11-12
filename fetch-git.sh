#!/bin/sh
# SPDX-License-Identifier: MIT
#
# Usage: fetch-git.sh <dir> <repo-url> <commit-sha>
#
# Clones <repo-url> into <dir> at exactly <commit-sha> (shallow). If <dir>
# already exists, verifies that its HEAD is <commit-sha> and refuses to
# continue otherwise, so a stale or tampered checkout is never built silently.
#
# Pinning to a full commit SHA (not a branch or tag name) means the upstream
# repository cannot change what gets built: moving or deleting branches or
# re-pointing tags has no effect.
set -e

dir="$1"
repo="$2"
commit="$3"

if [ -z "$dir" ] || [ -z "$repo" ] || [ -z "$commit" ]; then
	echo "usage: $0 <dir> <repo-url> <commit-sha>" >&2
	exit 2
fi

case "$commit" in
	*[!0-9a-f]*|"")
		echo ">> ERROR: '$commit' is not a lowercase hex commit SHA" >&2
		exit 2
		;;
esac
if [ ${#commit} -ne 40 ]; then
	echo ">> ERROR: commit must be a full 40-character SHA, got '$commit'" >&2
	exit 2
fi

if [ ! -d "$dir" ]; then
	echo ">> fetching $repo @ $commit"
	tmp="$dir.partial"
	rm -rf "$tmp"
	git init -q "$tmp"
	git -C "$tmp" remote add origin "$repo"
	git -C "$tmp" fetch -q --depth 1 origin "$commit"
	git -C "$tmp" -c advice.detachedHead=false checkout -q FETCH_HEAD
	# Only move into place once complete, so an interrupted fetch is retried.
	mv "$tmp" "$dir"
fi

head=$(git -C "$dir" rev-parse HEAD)
if [ "$head" != "$commit" ]; then
	echo ">> ERROR: $dir is at $head, expected pinned commit $commit" >&2
	echo ">> Remove '$dir' to re-fetch, or update the pin if this is intended." >&2
	exit 1
fi
echo ">> OK       $dir @ $commit"
