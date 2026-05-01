"""Manual router backend — no-op, just emits operator instructions.

Used when CatStack runs on a network whose router CatStack doesn't natively
integrate with (i.e. anything other than UniFi today). The dashboard renders
the messages from ApplyResult so the operator knows exactly which rule to
add by hand.
"""

from __future__ import annotations

from mfarm.router.base import ApplyResult, ForwardRule, RouterBackend


class ManualBackend(RouterBackend):
    name = "manual"
    requires_credentials = False

    def validate_config(self) -> None:
        return  # no required fields

    def test_connection(self) -> ApplyResult:
        return ApplyResult(
            ok=True,
            messages=["manual mode — no router connection to test"],
        )

    def apply_rule(self, rule: ForwardRule) -> ApplyResult:
        proto_label = {
            "tcp": "TCP",
            "udp": "UDP",
            "tcp_udp": "TCP and UDP",
        }.get(rule.protocol, rule.protocol)
        msg = (
            f"Add a port-forward rule on your router:\n"
            f"  Name:        Vast {rule.rig_name}\n"
            f"  External:    {rule.port_lo}-{rule.port_hi}\n"
            f"  Internal IP: {rule.internal_ip}\n"
            f"  Internal:    {rule.port_lo}-{rule.port_hi}\n"
            f"  Protocol:    {proto_label}"
        )
        return ApplyResult(ok=True, messages=[msg])

    def remove_rule(self, rig_name: str) -> ApplyResult:
        return ApplyResult(
            ok=True,
            messages=[f"Remove the 'Vast {rig_name}' rule from your router by hand."],
        )
