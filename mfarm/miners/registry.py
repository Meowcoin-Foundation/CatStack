"""Registry of supported miners and their CLI flag mappings.

The `supported_algos` list per miner is a FALLBACK only — used when live
discovery hasn't yet succeeded. Authoritative algorithm lists are derived
from the binary itself via `--list-algorithms` (or equivalent), parsed by
`parse_algo_output()` below, and cached server-side. See `discover_algos()`
in mfarm/web/api.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class MinerDefinition:
    name: str
    display_name: str
    binary_name: str
    supported_algos: list[str]      # fallback; live discovery preferred
    gpu_type: str                    # "nvidia", "amd", "cpu", "any"
    api_type: str                    # "ccminer_tcp", "trex_http", ...
    default_api_port: int
    supports_solo: bool = False

    # Live algorithm discovery. argv is appended after the binary path.
    # use_pty=True wraps in `script -qc ... /dev/null` (SRBMiner needs a TTY
    # or it falls into "Guided setup" interactive mode).
    algo_query_argv: list[str] | None = None
    algo_query_use_pty: bool = False

    @property
    def default_install_path(self) -> str:
        return f"/opt/mfarm/miners/{self.binary_name}"


# ── Algorithm-list output parsers ───────────────────────────────────
# Each parser takes the raw stdout of the miner's --list-algorithms (or
# equivalent) command and returns a sorted, deduped list of algo names.

_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub('', s)


def _parse_srbminer(raw: str) -> list[str]:
    # Lines like: "[0.85%]   [ C  A  N  - ]   yescryptr32"
    out = set()
    for line in raw.splitlines():
        line = _strip_ansi(line).strip()
        m = re.match(r'\[\s*[\d.]+%\]\s+\[[^\]]+\]\s+(\S+)', line)
        if m:
            out.add(m.group(1))
    return sorted(out)


def _parse_ccminer(raw: str) -> list[str]:
    # ccminer -h: tab-indented `algo<whitespace>description` block, starts
    # after "specify the hash algorithm to use" line, ends at next flag def
    # (line starting with "  -X,").
    out = set()
    in_block = False
    for line in raw.splitlines():
        bare = _strip_ansi(line)
        if 'specify the hash algorithm' in bare.lower():
            in_block = True
            continue
        if in_block:
            # End of block when we hit next flag definition.
            if re.match(r'\s\s-\w', bare) or re.match(r'\s\s--\w', bare):
                break
            stripped = bare.strip()
            if not stripped:
                continue
            # First whitespace-separated token is the algo name.
            tok = stripped.split()[0]
            # Sanity: algo names are short, lowercase-ish, can have digits/dashes/colons.
            if 1 <= len(tok) <= 32 and re.match(r'^[a-zA-Z0-9_:./+-]+$', tok):
                out.add(tok)
    return sorted(out)


def _parse_trex(raw: str) -> list[str]:
    # t-rex --help: under "-a, --algo  Specify the hash algorithm to use.",
    # one algo per line, deeply indented. Block ends at next flag (line whose
    # left-trim starts with `--` or `-X`).
    out = set()
    in_block = False
    for line in raw.splitlines():
        bare = _strip_ansi(line)
        if re.search(r'-a,\s*--algo\b', bare):
            in_block = True
            continue
        if in_block:
            stripped = bare.strip()
            if not stripped:
                continue
            # End of block: another flag definition. T-rex flags look like
            # "    -X, --xxx" or "        --xxx".
            if re.match(r'\s+-{1,2}[a-zA-Z]', bare) and stripped[0] == '-':
                break
            tok = stripped.split()[0]
            if 1 <= len(tok) <= 32 and re.match(r'^[a-zA-Z0-9_-]+$', tok):
                out.add(tok)
    return sorted(out)


def _parse_lolminer(raw: str) -> list[str]:
    # lolMiner --list-algos: ASCII-art banner then a table:
    #   Parameter        Algorithm               Fee %   Needs / Supports --pers
    #   ALEPH            Blake3-Alephium         0.75    false
    # First column (whitespace-separated) is the algo parameter.
    out = set()
    in_table = False
    for line in raw.splitlines():
        bare = _strip_ansi(line).rstrip()
        if 'Parameter' in bare and 'Algorithm' in bare:
            in_table = True
            continue
        if in_table:
            if not bare.strip():
                # blank line ends the table
                if out:
                    break
                continue
            tok = bare.split()[0] if bare.split() else ''
            if 1 <= len(tok) <= 32 and re.match(r'^[a-zA-Z0-9_-]+$', tok):
                out.add(tok)
    return sorted(out)


def _parse_miniz(raw: str) -> list[str]:
    # miniZ --help: lines like
    #   --par=[parameters]   Algorithm parameters: 144,5|125,4|150,5|kawpow
    #   --par=[parameters]   Algorithm parameters: MeowPow|firopow|...
    # Split right side of "Algorithm parameters:" on `|`.
    out = set()
    for line in raw.splitlines():
        bare = _strip_ansi(line)
        m = re.search(r'Algorithm parameters:\s*(.+)$', bare)
        if not m:
            continue
        for p in m.group(1).split('|'):
            tok = p.strip()
            if 1 <= len(tok) <= 32 and re.match(r'^[a-zA-Z0-9_,./+-]+$', tok):
                out.add(tok)
    return sorted(out)


def _parse_rigel(raw: str) -> list[str]:
    # rigel --help: under "Currently supported:" — one algo per line, format
    # "          algo_name (TICKER)". Block ends at blank line.
    out = set()
    in_block = False
    for line in raw.splitlines():
        bare = _strip_ansi(line)
        if 'Currently supported:' in bare:
            in_block = True
            continue
        if in_block:
            stripped = bare.strip()
            if not stripped:
                if out:
                    break
                continue
            # Match `algo_name  (XYZ, ...)` — first token before paren.
            m = re.match(r'([a-zA-Z0-9_-]+)\s*\(', stripped)
            if m:
                out.add(m.group(1))
    return sorted(out)


# Parser dispatch by miner name. Miners not in this table fall back to the
# hardcoded supported_algos list.
ALGO_PARSERS = {
    "srbminer":     _parse_srbminer,
    "ccminer":      _parse_ccminer,
    "cpuminer-opt": _parse_ccminer,   # same -h format as ccminer
    "trex":         _parse_trex,
    "lolminer":     _parse_lolminer,
    "miniz":        _parse_miniz,
    "rigel":        _parse_rigel,
}


def parse_algo_output(miner_name: str, raw: str) -> list[str]:
    parser = ALGO_PARSERS.get(miner_name.lower())
    if not parser:
        return []
    return parser(raw)


# ── Built-in miner definitions ──────────────────────────────────────

MINERS: dict[str, MinerDefinition] = {}


def _register(m: MinerDefinition):
    MINERS[m.name] = m


_register(MinerDefinition(
    name="ccminer",
    display_name="CCMiner",
    binary_name="ccminer",
    supported_algos=[
        "yescrypt", "yescryptR8", "yescryptR16", "yescryptR32",
        "scrypt", "scrypt:N", "sha256d", "sha256t",
        "keccak", "keccakc", "lyra2v2", "lyra2v3", "lyra2z",
        "neoscrypt", "x11", "x13", "x14", "x15", "x16r", "x16s",
        "x17", "qubit", "quark", "blake2s", "skein", "skein2",
        "groestl", "myr-gr", "lbry", "sib", "veltor",
        "hmq1725", "phi", "phi2", "tribus", "allium",
        "timetravel", "bitcore", "exosis", "hsr",
    ],
    gpu_type="nvidia",
    api_type="ccminer_tcp",
    default_api_port=4068,
    supports_solo=True,
    algo_query_argv=["-h"],
))

_register(MinerDefinition(
    name="cpuminer-opt",
    display_name="CPUMiner-Opt",
    binary_name="cpuminer",
    supported_algos=[
        "yescrypt", "yescryptR8", "yescryptR16", "yescryptR32",
        "scrypt", "scrypt:N", "sha256d", "sha3d",
        "x11", "x13", "x14", "x15", "x16r", "x16s", "x17",
        "lyra2v2", "lyra2v3", "lyra2z", "lyra2h",
        "qubit", "quark", "groestl", "myr-gr",
        "neoscrypt", "keccak", "keccakc",
        "blake2s", "skein", "skein2",
        "hmq1725", "phi", "phi2", "tribus", "allium",
        "anime", "argon2d-crds", "argon2d-dyn",
        "ghostrider", "minotaur", "minotaurx",
        "power2b", "verthash",
    ],
    gpu_type="cpu",
    api_type="ccminer_tcp",
    default_api_port=4048,
    supports_solo=True,
    algo_query_argv=["-h"],
))

_register(MinerDefinition(
    name="trex",
    display_name="T-Rex Miner",
    binary_name="t-rex",
    supported_algos=[
        "ethash", "etchash", "kawpow", "octopus",
        "autolykos2", "firopow", "progpow",
        "mtp", "tensority",
        "blake3", "sha256t",
    ],
    gpu_type="nvidia",
    api_type="trex_http",
    default_api_port=4067,
    supports_solo=False,
    algo_query_argv=["--help"],
))

_register(MinerDefinition(
    name="lolminer",
    display_name="lolMiner",
    binary_name="lolMiner",
    supported_algos=[
        "ethash", "etchash", "autolykos2",
        "beamhashiii", "equihash", "zhash",
        "cuckoo29", "cuckatoo31", "cuckatoo32",
        "etchash", "ton",
    ],
    gpu_type="any",
    api_type="lolminer_http",
    default_api_port=44444,
    supports_solo=False,
    algo_query_argv=["--list-algos"],
))

_register(MinerDefinition(
    name="xmrig",
    display_name="XMRig",
    binary_name="xmrig",
    supported_algos=[
        "randomx", "rx/0", "rx/wow", "rx/arq",
        "kawpow", "cn/r", "cn-heavy/xhv",
        "argon2/chukwa", "argon2/ninja",
        "ghostrider",
    ],
    gpu_type="any",
    api_type="xmrig_http",
    default_api_port=44445,
    supports_solo=False,
))


_register(MinerDefinition(
    name="miniz",
    display_name="miniZ",
    binary_name="miniZ",
    supported_algos=[
        "equihash144_5", "equihash192_7", "equihash210_9",
        "equihash125_4", "equihash150_5", "equihash96_5",
        "beamhashiii", "ethash", "etchash", "progpow",
        "octopus",
    ],
    gpu_type="nvidia",
    api_type="miniz_http",
    default_api_port=20000,
    supports_solo=False,
    algo_query_argv=["--help"],
))


_register(MinerDefinition(
    name="kerrigan",
    display_name="Kerrigan (custom Equihash192,7)",
    # Kerrigan's launcher is multi_gpu.sh, which spawns one mine.py + kerrigan_v4
    # daemon per GPU. We point binary_name at the launcher so miner_paths lookup
    # finds /opt/mfarm/miners/kerrigan/multi_gpu.sh.
    binary_name="kerrigan/multi_gpu.sh",
    supported_algos=["equihash192_7"],
    gpu_type="nvidia",
    # No HTTP/TCP API — the agent parses /var/log/mfarm/miner.log instead
    # (mine.py's stdout has per-GPU "XX.X I/s = YY.Y Sol/s" lines).
    api_type="kerrigan_log",
    default_api_port=0,
    supports_solo=False,
))


_register(MinerDefinition(
    name="srbminer",
    display_name="SRBMiner-Multi",
    binary_name="SRBMiner-Multi",
    # FALLBACK only — discovered list from --list-algorithms is preferred.
    # Kept here so the dropdown isn't empty if no rig is online for discovery.
    supported_algos=[
        "randomx", "rx/0", "rx/wow", "rx/arq",
        "ethash", "etchash", "autolykos2",
        "kawpow", "blake3", "sha256dt",
        "ghostrider", "dynamo", "yespower",
        "verthash", "heavyhash", "karlsenhash",
        "pyrinhash", "sha512_256d_radiant",
        "yescrypt", "yescryptr8", "yescryptr16", "yescryptr32",
    ],
    gpu_type="any",
    api_type="srbminer_http",
    default_api_port=21550,
    supports_solo=False,
    # Discovery: SRBMiner needs a PTY or it drops into "Guided setup".
    # `script -qc '<binary> --list-algorithms' /dev/null` is the wrapper.
    algo_query_argv=["--list-algorithms"],
    algo_query_use_pty=True,
))


_register(MinerDefinition(
    name="rigel",
    display_name="Rigel",
    binary_name="rigel",
    supported_algos=[
        "autolykos2", "ethash", "etchash", "kawpow", "octopus",
        "alephium", "ironfish", "kaspa", "karlsenhash", "karlsenhashv2",
        "nexapow", "pyrinhash", "pyrinhashv2",
        "xelishash", "xelishashv2", "xelishashv3",
        "sha512_256d_radiant",
    ],
    gpu_type="nvidia",
    api_type="rigel_http",
    default_api_port=4067,
    supports_solo=False,
    algo_query_argv=["--help"],
))


def get_miner(name: str) -> MinerDefinition | None:
    return MINERS.get(name.lower())


def list_miners() -> list[MinerDefinition]:
    return list(MINERS.values())
