#!/bin/bash
# mutate.sh LIST: prove each guard by mutation. LIST is a TSV of
#   name <TAB> file <TAB> perl substitution <TAB> packages (space-separated)
#   [<TAB> accepted: <why this survivor is acceptable>]
# For each line it applies the substitution to the file, runs the packages'
# tests, and restores the file from a copy. KILLED means a test failed;
# SURVIVED means every package passed with the guard broken; NOT-APPLIED
# means the substitution matched nothing, which is a stale list, not a pass.
# It exits 1 unless every mutation is KILLED or ACCEPTED.
set -u
list=${1:?usage: mutate.sh LIST}
killed=0; other=0
# A run stopped mid-mutation puts the file back, so an interrupted run never
# leaves broken source in the checkout.
current=""
restore() { if [ -n "$current" ] && [ -f "$current.mutate-orig" ]; then mv "$current.mutate-orig" "$current"; fi; }
trap restore EXIT
trap 'restore; exit 130' INT TERM HUP
while IFS=$'\t' read -r name file expr pkgs accepted; do
	case "$name" in ''|'#'*) continue;; esac
	# The original is copied whole, then renamed into place, and only then
	# named for restore: an interrupted copy is never moved over the source
	# (zeocouncil, PR 13 at 0a5138d9, INFO).
	cp "$file" "$file.mutate-tmp" && mv "$file.mutate-tmp" "$file.mutate-orig" || exit 1
	current=$file
	perl -0pi -e "$expr" "$file"
	if cmp -s "$file" "$file.mutate-orig"; then
		echo "NOT-APPLIED  $name"; other=$((other+1)); mv "$file.mutate-orig" "$file"; continue
	fi
	# shellcheck disable=SC2086
	out=$(go test -count=1 -timeout 180s $pkgs 2>&1)
	mv "$file.mutate-orig" "$file"
	if grep -q "\[build failed\]" <<<"$out"; then
		# Any package that did not build means the mutation did not compile
		# everywhere it was tested; that proves nothing, even beside a failing test.
		echo "BUILD-FAIL   $name"; other=$((other+1)); continue
	fi
	if grep -qE '^(FAIL|--- FAIL)' <<<"$out"; then
		by=$(grep -E '^--- FAIL' <<<"$out" | awk '{print $3}' | tr '\n' ' ')
		if [ -z "$by" ]; then by="(build failed: a mutation must compile to count)"; other=$((other+1)); echo "BUILD-FAIL   $name"; continue; fi
		echo "KILLED       $name  by: $by"; killed=$((killed+1))
	else
		# Only a fifth column that says "accepted: <why>" accepts a survivor;
		# anything else there is a malformed list, not a pass (zeocouncil, PR
		# 11 at 201c2446, INFO).
		case "$accepted" in
		'accepted: '?*) echo "ACCEPTED     $name  (${accepted#accepted: })";;
		'') echo "SURVIVED     $name"; other=$((other+1));;
		*) echo "SURVIVED     $name  (a fifth column that is not 'accepted: <why>' accepts nothing)"; other=$((other+1));;
		esac
	fi
done < "$list"
echo "mutations: $killed killed, $other not killed"
[ "$other" -eq 0 ]
