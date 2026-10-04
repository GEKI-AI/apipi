#!/bin/sh
file=$1
op=$2
[ "$op" = get ] || exit 0
protocol=
host=
while IFS= read -r line; do
  [ -n "$line" ] || break
  case $line in
    protocol=*) protocol=${line#protocol=} ;;
    host=*) host=${line#host=} ;;
  esac
done
[ "$protocol" = https ] || exit 0
[ -n "$host" ] || exit 0
[ -f "$file" ] || exit 0
host=$(printf '%s' "$host" | tr '[:upper:]' '[:lower:]')
tab=$(printf '\t')
while IFS=$tab read -r name user secret; do
  if [ "$name" = "$host" ]; then
    printf 'username=%s\npassword=%s\n' "$user" "$secret"
    exit 0
  fi
done < "$file"
exit 0
