from __future__ import annotations

from .sip import DomonapSipCall


class RubetekPanelSipCall(DomonapSipCall):
    """SIP session used by the Rubetek Panel profile.

    The full dialog lifecycle (REGISTER, INVITE, 200 OK answer with a
    no-media SDP, ACK tracking, BYE/603 teardown and REGISTER Expires: 0)
    lives in ``DomonapSipCall`` and matches the app: answering mutes the
    intercom panel without any media in Home Assistant.

    This subclass exists as the panel-profile marker: the shared client owns
    registration and teardown, while ``PanelCallController`` may bridge an
    established call to Asterisk by passing its SDP answer through
    ``answer_with_sdp()`` unchanged. It never proxies RTP itself.

    The APK treats the SIP account as a per-call object: after terminating the
    current dialog it disables registration/removes the account and stops the SIP
    core. ``destroy()`` mirrors that behavior by terminating the dialog, sending
    REGISTER with Expires: 0 and only then closing the TCP session.
    """
