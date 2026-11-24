#!/usr/bin/env bash
# הוספת מפתחות ג'ימיני: מדביקים, הסקריפט בודק שהמפתח עובד, מוסיף אותו
# לרשימה, ומפעיל את הבוט מחדש רק כשאין עבודה פעילה — אף עבודה לא נקטעת.
#
#   bash /opt/zovexsub/repo/bot/addkey.sh
set -u
BOT="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$BOT/.env"
ACTIVE="${WORK_DIR:-/tmp/zovexsub}/active.json"
PY="/opt/zovexsub/venv/bin/python"
[[ -x "$PY" ]] || PY=python3

if [[ ! -f "$ENV_FILE" ]]; then
  echo "❌ לא נמצא $ENV_FILE"; exit 1
fi

added=0
while true; do
  echo
  read -r -p "🔑 הדבק מפתח ג'ימיני (Enter ריק = סיום): " KEY
  KEY="$(echo "$KEY" | tr -d '[:space:]"'"'")"
  [[ -z "$KEY" ]] && break

  printf "   בודק מול גוגל… "
  CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "x-goog-api-key: $KEY" \
    "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1")
  if [[ "$CODE" != "200" ]]; then
    echo "❌ המפתח לא עובד (קוד $CODE) — לא נוסף"
    continue
  fi
  echo "✅ עובד"

  RESULT=$("$PY" - "$ENV_FILE" "$KEY" <<'EOF'
import sys
path, key = sys.argv[1], sys.argv[2]
lines = open(path, encoding="utf-8").read().splitlines()
idx = next((i for i, l in enumerate(lines) if l.startswith("GEMINI_API_KEYS=")), None)
old = []
if idx is not None:
    old = [k.strip() for k in lines[idx].split("=", 1)[1].strip().strip('"').split(",") if k.strip()]
if key in old:
    print(f"dup {len(old)}")
    sys.exit()
keys = old + [key]
row = "GEMINI_API_KEYS=" + ",".join(keys)
if idx is None:
    lines.append(row)
else:
    lines[idx] = row
open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
print(f"ok {len(keys)}")
EOF
)
  case "$RESULT" in
    dup*) echo "   ⚠️ המפתח כבר ברשימה (${RESULT#dup } מפתחות)";;
    ok*)  echo "   ➕ נוסף — עכשיו ${RESULT#ok } מפתחות"; added=$((added + 1));;
    *)    echo "   ❌ ההוספה נכשלה: $RESULT";;
  esac
done

if [[ $added -eq 0 ]]; then
  echo "לא נוספו מפתחות חדשים — אין צורך בהפעלה מחדש."
  exit 0
fi

# מחכים שלא תרוץ שום עבודה. active.json נכתב על ידי הבוט עצמו
busy() { [[ -s "$ACTIVE" ]] && grep -q '[0-9]' "$ACTIVE"; }
if busy; then
  echo
  echo "⏳ עבודה רצה כרגע בבוט. ממתין שתסתיים ואז מפעיל מחדש (Ctrl+C לביטול —"
  echo "   המפתחות כבר שמורים, ויכנסו לפעולה בהפעלה מחדש הבאה)."
  while busy; do sleep 30; printf "."; done
  echo
fi
systemctl restart zovexsub && sleep 5
if systemctl is-active --quiet zovexsub; then
  echo "✅ הבוט הופעל מחדש עם $added מפתחות חדשים."
else
  echo "❌ הבוט לא עלה אחרי ההפעלה מחדש — בדוק: journalctl -u zovexsub -n 30"
fi
