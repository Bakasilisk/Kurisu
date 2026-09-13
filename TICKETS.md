# Tickets — Anleitung für Mitglieder

Ein Ticket ist ein privates Gespräch mit dem Mod-Team. Du schreibst es im Server auf,
alles Weitere läuft per DM mit dem Bot.

## 1. Ticket öffnen

Im Server (nicht per DM):

```
/ticket open Ich werde von @jemand belästigt, hier ist was passiert …
```

`/ticket open` ist die empfohlene Variante: deine Nachricht und die Bestätigung sind
**nur für dich sichtbar**. `.ticket open <Text>` funktioniert genauso, ist aber für alle
im Kanal lesbar — dafür kannst du damit **Dateien/Screenshots anhängen**, was per `/`
nicht geht.

- Höchstens **1 offenes Ticket pro Server** (auf verschiedenen Servern gleichzeitig ist okay).
- Höchstens **1500 Zeichen** pro Nachricht.
- Der Bot schickt dir sofort eine DM: `🎫 Ticket #N opened`.

**Wichtig:** Du musst DMs von Server-Mitgliedern erlauben, sonst erreichen dich keine
Antworten. Server → Rechtsklick → *Privatsphäre-Einstellungen* → *Direktnachrichten von
Server-Mitgliedern erlauben*. Wenn das aus ist, sagt der Bot dir das beim Öffnen.

## 2. Antworten

Sobald das Ticket offen ist, **schreib dem Bot einfach eine DM** — kein Befehl nötig.
Jede DM wird automatisch an dein Ticket weitergeleitet, der Bot bestätigt mit
`📨 Forwarded to ticket #N`. Screenshots und Dateien kannst du direkt anhängen
(bis 8 MB, größere werden nur namentlich erwähnt).

Ausnahme: Wenn du auf **mehreren Servern** gleichzeitig ein Ticket offen hast, weiß der
Bot nicht, welches du meinst. Dann wähle es explizit:

```
.ticket reply 12 Hier der Screenshot von gestern
```

Kleine Bremse: eine Nachricht alle 5 Sekunden. Ein Ticket fasst 200 Nachrichten.

## 3. Antworten vom Mod-Team

Antworten kommen als DM mit dem Titel **„Reply from the mod team"** — ohne Namen,
Avatar oder Ping des Mods. Das ist Absicht: du erfährst nicht, wer geantwortet hat.

Umgekehrt gilt das **nicht**: dein Name, dein Text und deine Anhänge sind für das
Mod-Team im Ticket-Kanal sichtbar. Ein Ticket ist vertraulich, aber nicht anonym.

## 4. Ticket schließen

Du kannst dein eigenes Ticket jederzeit selbst schließen — in der DM oder im Server:

```
.ticket close 12
.ticket close 12 Hat sich erledigt, danke
```

Das Mod-Team kann es ebenfalls schließen; du bekommst dann eine DM
`🔒 Ticket #N closed` (mit Grund, falls einer angegeben wurde). Geschlossene Tickets
werden nach 30 Tagen gelöscht. Danach kannst du ein neues öffnen.

## Kurzübersicht

| Was | Wie | Wo |
|---|---|---|
| Ticket öffnen | `/ticket open <Text>` | im Server |
| Ticket öffnen mit Anhang | `.ticket open <Text>` + Datei | im Server |
| Nachschieben | einfach eine DM an den Bot schreiben | DM |
| Nachschieben bei mehreren Tickets | `.ticket reply <Nr> <Text>` | DM |
| Schließen | `.ticket close <Nr> [Grund]` | DM oder Server |
| Hilfe anzeigen | `/ticket` | überall |

## Wenn etwas nicht klappt

| Bot sagt | Heißt |
|---|---|
| *Tickets aren't set up on this server* | Der Server hat noch keinen Ticket-Kanal — melde dich bei einem Mod. |
| *You already have open ticket #N* | Schreib in dieses Ticket weiter (DM) oder schließ es zuerst. |
| *I couldn't DM you* | Deine DMs sind zu — siehe Schritt 1, sonst siehst du keine Antworten. |
| *You have no open ticket* | Deine DM gehört zu keinem Ticket; öffne erst eins im Server. |
| *This ticket is full* | 200 Nachrichten erreicht — bitte einen Mod, es zu schließen, dann neu öffnen. |
| *Reply to me in DM to add to your ticket* | `.ticket reply` im Server ist Mods vorbehalten; nutze die DM. |
