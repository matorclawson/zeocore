#!/bin/bash
# mutate.sh LIST: prove each guard by mutation. LIST is a TSV of
#   name <TAB> file <TAB> perl substitution <TAB> packages (space-separated)
#   [<TAB> accepted: <why this survivor is acceptable>]
# For each line it applies the substitution to the file IN A SCRATCH COPY of the
# module, runs the packages' tests there, and reports. KILLED means a test
# failed; SURVIVED means every package passed with the guard broken;
# NOT-APPLIED means the substitution matched nothing, which is a stale list, not
# a pass. It exits 1 unless every mutation is KILLED or ACCEPTED.
#
# The checkout is never touched: each row works on its own copy, so rows run in
# parallel (MUTATE_JOBS, default the CPU count, at most 4; MUTATE_JOBS=1 is the
# serial audit mode) and an interrupted run
# leaves nothing to restore. Before any row, the UNMUTATED copy must pass the
# same tests: a copy that cannot run them (a path outside the module, say) would
# otherwise fail every row and "kill" every mutation for the wrong reason.
set -u

if [ -n "${MUTATE_ROW:-}" ]; then
	# Worker: one row, in its own copy. Prints one line to $MUTATE_TMP/out.$MUTATE_ROW.
	i=$MUTATE_ROW
	src=$PWD
	row=$(sed -n "${i}p" "$1")
	IFS=$'\t' read -r name file expr pkgs accepted <<<"$row"
	d="$MUTATE_TMP/w$i"
	mkdir "$d" && cp -R "$src"/. "$d" || { echo "BUILD-FAIL   $name  (the scratch copy failed)" >"$MUTATE_TMP/out.$i"; exit 0; }
	cd "$d" || exit 0
	perl -0pi -e "$expr" "$file"
	if cmp -s "$file" "$src/$file"; then
		echo "NOT-APPLIED  $name" >"$MUTATE_TMP/out.$i"
		rm -rf "$d"
		exit 0
	fi
	# shellcheck disable=SC2086
	out=$(go test -count=1 -timeout 180s $pkgs 2>&1)
	if grep -q "\[build failed\]" <<<"$out"; then
		# Any package that did not build means the mutation did not compile
		# everywhere it was tested; that proves nothing, even beside a failing test.
		echo "BUILD-FAIL   $name" >"$MUTATE_TMP/out.$i"
	elif grep -qE '^(FAIL|--- FAIL)' <<<"$out"; then
		by=$(grep -E '^--- FAIL' <<<"$out" | awk '{print $3}' | tr '\n' ' ')
		if [ -z "$by" ]; then
			echo "BUILD-FAIL   $name" >"$MUTATE_TMP/out.$i"
		else
			echo "KILLED       $name  by: $by" >"$MUTATE_TMP/out.$i"
		fi
	else
		# Only a fifth column that says "accepted: <why>" accepts a survivor;
		# anything else there is a malformed list, not a pass (zeocouncil, PR
		# 11 at 201c2446, INFO).
		case "$accepted" in
		'accepted: '?*) echo "ACCEPTED     $name  (${accepted#accepted: })" >"$MUTATE_TMP/out.$i";;
		'') echo "SURVIVED     $name" >"$MUTATE_TMP/out.$i";;
		*) echo "SURVIVED     $name  (a fifth column that is not 'accepted: <why>' accepts nothing)" >"$MUTATE_TMP/out.$i";;
		esac
	fi
	rm -rf "$d"
	exit 0
fi

list=${1:?usage: mutate.sh LIST}
self="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
tmp=$(mktemp -d "${TMPDIR:-/tmp}/mutate.XXXXXX") || exit 1
trap 'rm -rf "$tmp"' EXIT
trap 'rm -rf "$tmp"; exit 130' INT TERM HUP

jobs=${MUTATE_JOBS:-}
if [ -z "$jobs" ]; then
	jobs=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 1)
fi
case "$jobs" in ''|*[!0-9]*) jobs=1;; esac
[ "$jobs" -gt 4 ] && jobs=4
[ "$jobs" -lt 1 ] && jobs=1

# The rows, without blank lines and comments.
grep -v -E '^([[:space:]]*#|[[:space:]]*$)' "$list" >"$tmp/rows"
n=$(wc -l <"$tmp/rows" | tr -d ' ')

# The unmutated copy must pass.
mkdir "$tmp/base" && cp -R . "$tmp/base" || { echo "BASELINE-FAIL  the scratch copy failed"; exit 1; }
if ! (cd "$tmp/base" && go test -count=1 -timeout 300s ./... >"$tmp/base.log" 2>&1); then
	echo "BASELINE-FAIL  the unmutated copy does not pass its own tests; no mutation proves anything"
	tail -n 20 "$tmp/base.log"
	exit 1
fi
rm -rf "$tmp/base"

# One worker per row, $jobs at a time.
seq 1 "$n" | xargs -P "$jobs" -I{} env MUTATE_ROW={} MUTATE_TMP="$tmp" "$self" "$tmp/rows"

killed=0; other=0
i=1
while [ "$i" -le "$n" ]; do
	if [ -f "$tmp/out.$i" ]; then
		line=$(cat "$tmp/out.$i")
	else
		line="BUILD-FAIL   (row $i produced no result)"
	fi
	echo "$line"
	case "$line" in
	KILLED*) killed=$((killed+1));;
	ACCEPTED*) ;;
	*) other=$((other+1));;
	esac
	i=$((i+1))
done
echo "mutations: $killed killed, $other not killed"
[ "$other" -eq 0 ]
