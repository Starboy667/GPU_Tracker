
import pytest
import requests
import responses

from pricewatch import outbound
from pricewatch.outbound import OutboundMonitor, OutboundStatus, parse

URL = "https://check.example/json"
STAMP = "2026-11-25T09:00:00Z"
MULLVAD = {"ip": "193.32.126.172", "country": "France", "city": "Paris", "mullvad_exit_ip": True,
           "mullvad_exit_ip_hostname": "fr-par-wg-004", "organization": "31173 Services AB"}


def test_parse_mullvad_answer():
    assert parse(MULLVAD, STAMP) == OutboundStatus(
        checked_at=STAMP, ok=True, ip="193.32.126.172", location="Paris, France",
        organization="31173 Services AB", vpn=True, vpn_server="fr-par-wg-004")
    direct = parse({**MULLVAD, "mullvad_exit_ip": False, "mullvad_exit_ip_hostname": None}, STAMP)
    assert direct.ok and direct.vpn is False and direct.vpn_server == ""


def test_parse_generic_answer_does_not_claim_a_vpn():
    status = parse({"ip": "2001:db8::1", "city": "Roubaix", "country": "FR", "org": "AS16276 OVH SAS"}, STAMP)
    assert status.ok and status.vpn is None
    assert (status.ip, status.location, status.organization) == ("2001:db8::1", "Roubaix, FR", "AS16276 OVH SAS")


@pytest.mark.parametrize("payload", [None, [], "text", {}, {"ip": "not an address"}, {"ip": 42}])
def test_parse_refuses_nonsense(payload):
    status = parse(payload, STAMP)
    assert not status.ok and status.error == "unexpected answer"


def test_parse_keeps_untrusted_text_short_and_single_line():
    status = parse({"ip": "1.2.3.4", "city": "Paris\n<script>" + "x" * 500, "mullvad_exit_ip": "yes"}, STAMP)
    assert len(status.location) <= outbound.MAX_FIELD_LENGTH and "\n" not in status.location
    assert status.vpn is None  # only a real boolean counts


def test_monitor_is_off_without_a_url(clock):
    monitor = OutboundMonitor("", clock=clock)
    assert not monitor.enabled and monitor.current() is None


def test_monitor_caches_good_answers_and_retries_failures_sooner(clock):
    answers = [MULLVAD, outbound.OutboundError("cannot connect"), {**MULLVAD, "ip": "193.32.126.173"}]
    calls = []

    def fetch(url):
        calls.append(url)
        answer = answers[len(calls) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monitor = OutboundMonitor(URL, clock=clock, fetch=fetch)
    first = monitor.current()
    assert first.ok and first.vpn and first.checked_at == STAMP
    clock.advance(minutes=4)
    assert monitor.current() is first and len(calls) == 1  # still fresh

    clock.advance(minutes=2)
    down = monitor.current()
    assert not down.ok and down.error == "cannot connect" and len(calls) == 2
    clock.advance(seconds=30)
    assert monitor.current() is down and len(calls) == 2  # not hammered while down
    clock.advance(seconds=40)
    assert monitor.current().ip == "193.32.126.173" and len(calls) == 3


def test_monitor_survives_a_crashing_check(clock):
    def boom(url):
        raise RuntimeError("secret detail")

    status = OutboundMonitor(URL, clock=clock, fetch=boom).current()
    assert not status.ok and status.error == "check failed (RuntimeError)"


@responses.activate
def test_fetch_reads_json(public_dns):
    responses.get(URL, json=MULLVAD)
    assert outbound._fetch_json(URL) == MULLVAD
    assert responses.calls[0].request.headers["Accept"] == "application/json"


@responses.activate
@pytest.mark.parametrize("failure,message", [
    (requests.ConnectTimeout("x"), "no answer within a few seconds"),
    (requests.ConnectionError("x"), "cannot connect"),
    (requests.TooManyRedirects("x"), "check failed (TooManyRedirects)"),
])
def test_fetch_failures_are_reported_plainly(public_dns, failure, message):
    responses.get(URL, body=failure)
    with pytest.raises(outbound.OutboundError, match=message.replace("(", r"\(").replace(")", r"\)")):
        outbound._fetch_json(URL)


@responses.activate
def test_fetch_rejects_bad_statuses_bodies_and_sizes(public_dns):
    responses.get(URL, status=503)
    with pytest.raises(outbound.OutboundError, match="HTTP 503"):
        outbound._fetch_json(URL)
    responses.replace(responses.GET, URL, body="<html>captive portal</html>")
    with pytest.raises(outbound.OutboundError, match="unexpected answer"):
        outbound._fetch_json(URL)
    responses.replace(responses.GET, URL, body="x" * (outbound.MAX_RESPONSE_BYTES + 1))
    with pytest.raises(outbound.OutboundError, match="ResponseTooLarge"):
        outbound._fetch_json(URL)


def test_fetch_refuses_private_addresses():
    with pytest.raises(outbound.OutboundError):
        outbound._fetch_json("http://127.0.0.1:9/json")
