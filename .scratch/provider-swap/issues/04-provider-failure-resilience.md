# 04: Provider failure resilience

**What to build:** When a Provider's list call fails, or starts answering 403 or "Violation", the app keeps serving that Provider's last good game list for a limited number of hours and carries on with any other Provider. A failed fetch never removes or rebuilds Channels, in line with the rule that the sync never changes Channels when it cannot read its inputs. The failure is logged and shown in the dashboard.

**Blocked by:** 03

**Status:** implemented on provider-swap (awaiting review)

- [x] When a Provider's list call fails, the served playlist still contains its last good games, checked through the test seam.
- [x] After the retention limit passes, the stale list stops being served, and Channels are still not removed or rebuilt because of the failure.
- [x] A 403 or Violation answer is treated as a failure, logged, and does not crash the refresh.
- [x] One Provider failing does not affect games from the other Provider.
- [x] The dashboard shows which Provider is failing and when it last succeeded.
- [x] The retention limit is a setting.
- [x] Existing checks still pass on a scratch-tagged image.
