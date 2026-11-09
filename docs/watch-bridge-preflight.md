# Android MX Player watch bridge — blocked preflight

Status: **blocked; no watcher has been implemented or validated**. Tracked by
[#14](https://github.com/mbtiongson1/kura/issues/14), the independently authorized
slice of #5, #6 and #11. Baseline: `c42b194ce3e623732cc2844da5b8f5ce2ce00fe2`.
No `/watch` command is available from this report. Do not merge this report as a
completed implementation.

## Device evidence

Read-only checks from native Termux on the requested phone returned:

- `command -v adb`: no executable found.
- `/system/bin/dumpsys media_session`: `Permission Denial` for missing
  `android.permission.DUMP`.
- `/system/bin/dumpsys activity activities`: the same missing `DUMP` permission.
- `/system/bin/cmd media_session list-sessions`: `***Error listing sessions***`.

No playback action, app restart, screen scraping, device pairing, or permission
change was performed. These failures provide **no evidence that MX Player is or is
not foregrounded or playing**. Title, URI, position, duration, state, completion,
and mid-session attach are all unverified. There is no basis to claim a session,
start time, foreground duration, or episode completion. Termux background survival,
polling CPU and battery impact were not measured.

The direct Termux UID is not the ADB shell UID. An authorized ADB shell could expose
additional information, but that has not been demonstrated on this device; the format,
availability and semantics must be measured before choosing an implementation. A
foreground signal, if available, would be activity evidence, not proven playback.

## Setup needed to continue

1. In Termux install Android platform tools: `pkg install android-tools` (confirm the
   package prompt). Do not publish ADB keys, pairing codes or command output containing
   private media metadata.
2. On Android, enable Developer options, then **Wireless debugging**. On a trusted
   network select **Pair device with pairing code**. In Termux run
   `adb pair <pairing-IP>:<pairing-port>` and enter the displayed code. Close the
   pairing screen and use the distinct connection address shown on the Wireless
   debugging main screen: `adb connect <IP>:<connection-port>`. Same-device pairing
   and connectivity can vary; if unavailable use an explicitly authorized trusted
   workstation, or revisit a permissioned native companion. Do not expose ADB to an
   untrusted network. Disable wireless debugging when finished.
3. While MX Player Pro is already playing and without controlling it, test
   `adb devices`, `adb shell dumpsys activity activities` and
   `adb shell dumpsys media_session`. The output can contain private filenames and
   metadata: inspect locally and share only the minimum necessary. Verify that the
   exact package `com.mxtech.videoplayer.pro` is associated with actual observable
   evidence; distinguish absent metadata from paused playback and denied access.
4. Only after a suitable observable signal is verified should the deterministic
   observer, state machine, private additive SQLite migrations, Pi command and tests be
   implemented. Validate transitions with user cooperation without interrupting or
   relaunching playback; also test owner death, crash recovery, pending confirmation,
   existing download regression and real Android background behavior. Independent
   review of the pushed implementation and passing checks remain required for merge.

Wireless debugging and its pairing flow are described by
[Android's ADB documentation](https://developer.android.com/tools/adb). The player’s
[official intent API](https://sites.google.com/site/mxvpen/api) describes a launch
contract and result handling for a cooperating caller; it cannot attach retroactively
to the existing session. Android's
[UsageStatsManager](https://developer.android.com/reference/android/app/usage/UsageStatsManager)
requires appropriate user-granted access for usage data; a
[NotificationListenerService](https://developer.android.com/reference/android/service/notification/NotificationListenerService)
requires an installed native service and user authorization. Neither alternative was
installed or tested, nor is either evidence that MX Player exposes metadata. Android
can kill background processes; a terminal wake lock or battery exception does not prove
survival. See [process lifecycle](https://developer.android.com/guide/components/activities/process-lifecycle)
and [Android 15 behavior changes](https://developer.android.com/about/versions/15/behavior-changes-all).

## Repository and cloud boundaries

The existing `Store` uses private SQLite permissions, schema version 1, and an
operation lock for acquisition mutations. Any later watch migration must be additive,
transactional, preserve all receipts, and never hold that lock for a watch lifetime.
The installed Pi extension API documents `registerCommand` and `session_shutdown`;
lifecycle implementation and abrupt-parent-death behavior remain to be tested.

Existing regression suite: `PYTHONPATH=src python -m unittest discover -s tests -v`
passed all 54 tests. An initial invocation without installation or `PYTHONPATH`
failed during import; it did not exercise the tests. No new watch tests exist.

The Supabase CLI was not found in PATH. No credentials were read or printed. Supabase,
RLS, outbox and AniList writes remain unimplemented; cloud prerequisites are not a
blocker for the local MVP. This preflight made no repository runtime changes.
