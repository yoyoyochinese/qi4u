"""Client for Korean Government OpenAPI – ER real-time bed info.

Endpoint: getEmrrmRltmUsefulSckbdInfoInqire
Docs: https://www.data.go.kr/data/15000563/openapi.do

Fetches ER capacity data for Busan hospitals, caches in-memory per run.
"""

import xml.etree.ElementTree as ET
from typing import Optional

import httpx

from src.models.hospital import Hospital

_BASE_URL = (
    "http://apis.data.go.kr/B552657/ErmctInfoInqireService"
    "/getEmrrmRltmUsefulSckbdInfoInqire"
)

# Busan metropolitan city stage1/stage2 codes used in data.go.kr
# STAGE1 = "26" is Busan광역시
_BUSAN_STAGE1 = "26"
_BUSAN_HP_PREFIX = "A26"
_BUSAN_ADDR_KEYWORD = "부산"

# In-memory cache: populated once per process lifetime (or reset explicitly)
_cached_hospitals: Optional[list[Hospital]] = None


# Fallback lat/lng for known Busan ER hospitals (approximate).
# Used when the API response does not include coordinates.
# Source: manual lookup of major Busan ER-equipped hospitals.
BUSAN_HOSPITAL_COORDS: dict[str, tuple[float, float]] = {
    "부산대학교병원": (35.2338, 129.0803),
    "인제대학교 부산백병원": (35.1007, 129.0260),
    "인제대학교 해운대백병원": (35.1700, 129.1755),
    "동아대학교병원": (35.0955, 129.0185),
    "고신대학교복음병원": (35.0770, 129.0220),
    "부산의료원": (35.1245, 129.1010),
    "좋은강안병원": (35.1500, 129.1200),
    "메리놀병원": (35.1120, 129.0350),
    "온종합병원": (35.1390, 129.0590),
    "부민병원": (35.0960, 129.0200),
    "대동병원": (35.1330, 129.0700),
    "부산성모병원": (35.1150, 129.0400),
    "좋은삼선병원": (35.1340, 129.0530),
    "복음병원": (35.0770, 129.0220),
    "동래봉생병원": (35.2050, 129.0850),
    "해운대부민병원": (35.1630, 129.1630),
    "센텀병원": (35.1720, 129.1300),
    "부산보훈병원": (35.0962, 128.9673),
    "김해중앙병원": (35.2350, 128.8850),
    "양산부산대학교병원": (35.3390, 129.0163),
    "인제대학교 상계백병원": (35.1050, 129.0300),
    "일신기독병원": (35.0890, 129.0420),
    "부산광역시의료원": (35.1245, 129.1010),
    "좋은문화병원": (35.1630, 129.0580),
    "김원묵기념봉생병원": (35.1190, 129.0370),
    "명지병원": (35.1600, 129.1700),
    "마산의료원": (35.1800, 128.5700),
    "창원파티마병원": (35.2300, 128.6800),
}

# Default coordinates for hospitals not in the lookup (Busan city center)
_DEFAULT_LAT = 35.1796
_DEFAULT_LNG = 129.0756
_TARGET_FALLBACK_HOSPITALS = 38


def _find_coords(name: str) -> tuple[float, float]:
    """Best-effort coordinate lookup by hospital name substring match."""
    for key, coords in BUSAN_HOSPITAL_COORDS.items():
        if key in name or name in key:
            return coords
    return (_DEFAULT_LAT, _DEFAULT_LNG)


def _is_busan_hospital(*, hpid: str, name: str, addr: str, lat: str, lng: str) -> bool:
    """Filter out non-Busan rows when upstream region filtering is inconsistent."""
    if hpid.startswith(_BUSAN_HP_PREFIX):
        return True

    text = f"{name} {addr}"
    if _BUSAN_ADDR_KEYWORD in text:
        return True

    # Keep geo-bounded entries when coordinates are present.
    if lat and lng:
        try:
            lat_f = float(lat)
            lng_f = float(lng)
            if 35.0 <= lat_f <= 35.35 and 128.9 <= lng_f <= 129.35:
                return True
        except ValueError:
            pass

    return False


def _jitter_center_coords(hospital_id: str) -> tuple[float, float]:
    """Spread default-center markers so hospitals remain visible on the map."""
    offset = sum(ord(ch) for ch in hospital_id) % 61
    lat = _DEFAULT_LAT + ((offset % 11) - 5) * 0.0016
    lng = _DEFAULT_LNG + ((offset // 11) - 2) * 0.0022
    return (lat, lng)


def _parse_xml_response(xml_text: str) -> list[Hospital]:
    """Parse the XML response from getEmrrmRltmUsefulSckbdInfoInqire."""
    root = ET.fromstring(xml_text)

    # Check for error
    header = root.find(".//header")
    if header is not None:
        result_code = header.findtext("resultCode", "")
        if result_code != "00":
            msg = header.findtext("resultMsg", "unknown error")
            raise RuntimeError(f"API error {result_code}: {msg}")

    items = root.findall(".//item")
    hospitals: list[Hospital] = []

    seen_hospital_ids: set[str] = set()
    for item in items:
        hpid = item.findtext("hpid", "")
        name = item.findtext("dutyName", "Unknown")
        addr = item.findtext("dutyAddr", "")
        wgs84_lat = item.findtext("wgs84Lat", "")
        wgs84_lng = item.findtext("wgs84Lon", "")

        if not _is_busan_hospital(
            hpid=hpid,
            name=name,
            addr=addr,
            lat=wgs84_lat,
            lng=wgs84_lng,
        ):
            continue

        # hvec = ER available beds, hvs01~hvs59 = specialty beds
        # We use hvec (ER beds available) as the primary capacity indicator
        # and hvidate for timestamp
        er_available = item.findtext("hvec", "0")  # ER available beds
        # Total ER beds = we estimate from the available count + assume ~70-85% occupied
        # A better field is the total capacity, but this API primarily gives available counts.

        try:
            available = max(0, int(er_available))
        except (ValueError, TypeError):
            available = 0

        # Estimate max capacity: available beds represent the remaining capacity.
        # We derive total capacity by assuming a base occupancy rate.
        # If available=0, we set a minimum capacity of 5 (small ER).
        # Formula: max_capacity = max(available * 4, 5)
        # This means if 5 beds available, total ~20; occupancy ~15 (75%).
        # Rationale: Korean ER departments typically have 10-50 beds; average ~20.
        if available > 0:
            max_capacity = max(available * 4, 8)
        else:
            max_capacity = 8

        current_occupancy = max_capacity - available

        if wgs84_lat and wgs84_lng:
            try:
                lat = float(wgs84_lat)
                lng = float(wgs84_lng)
            except ValueError:
                lat, lng = _find_coords(name)
        else:
            lat, lng = _find_coords(name)

        hospital_id = hpid or f"hosp_{len(hospitals)}"
        if hospital_id in seen_hospital_ids:
            continue
        seen_hospital_ids.add(hospital_id)

        if lat == _DEFAULT_LAT and lng == _DEFAULT_LNG:
            lat, lng = _jitter_center_coords(hospital_id)

        hospitals.append(
            Hospital(
                hospital_id=hospital_id,
                hospital_name=name,
                lat=lat,
                lng=lng,
                max_capacity=max_capacity,
                current_occupancy=current_occupancy,
            )
        )

    return hospitals


async def fetch_busan_hospitals(service_key: str) -> list[Hospital]:
    """Fetch ER hospital data for Busan from data.go.kr.

    Makes one HTTP request, parses XML, returns Hospital list.
    Results are cached in-memory after first call.
    """
    global _cached_hospitals
    if _cached_hospitals is not None:
        return _cached_hospitals

    params = {
        "serviceKey": service_key,
        "STAGE1": _BUSAN_STAGE1,
        "STAGE2": "",
        "pageNo": "1",
        "numOfRows": "100",  # fetch all Busan hospitals in one page
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(_BASE_URL, params=params)
        resp.raise_for_status()

    hospitals = _parse_xml_response(resp.text)

    if not hospitals:
        raise RuntimeError(
            "No hospitals returned from API. Check service key and parameters."
        )

    _cached_hospitals = hospitals
    return hospitals


def generate_fallback_hospitals() -> list[Hospital]:
    """Generate hospitals from built-in Busan coordinates.

    Used when no API key is available (demo / testing mode).
    Assigns realistic capacity values and moderate-to-high occupancy.
    """
    import random

    rng = random.Random(7777)
    hospitals: list[Hospital] = []
    # Use only hospitals clearly within Busan metro area
    busan_only = {
        k: v
        for k, v in BUSAN_HOSPITAL_COORDS.items()
        if 35.0 <= v[0] <= 35.35 and 128.9 <= v[1] <= 129.25
    }
    for i, (name, (lat, lng)) in enumerate(busan_only.items()):
        max_cap = rng.randint(12, 40)
        occ = rng.randint(int(max_cap * 0.55), int(max_cap * 0.85))
        hospitals.append(
            Hospital(
                hospital_id=f"fallback_{i}",
                hospital_name=name,
                lat=lat,
                lng=lng,
                max_capacity=max_cap,
                current_occupancy=occ,
            )
        )

    # Keep demo behavior stable when API is unavailable: always provide 38 hospitals.
    base_count = len(hospitals)
    while len(hospitals) < _TARGET_FALLBACK_HOSPITALS:
        idx = len(hospitals)
        lat = BUSAN_HOSPITAL_COORDS["부산대학교병원"][0] + rng.uniform(-0.09, 0.09)
        lng = BUSAN_HOSPITAL_COORDS["부산대학교병원"][1] + rng.uniform(-0.12, 0.12)
        max_cap = rng.randint(10, 34)
        occ = rng.randint(int(max_cap * 0.60), int(max_cap * 0.92))
        hospitals.append(
            Hospital(
                hospital_id=f"fallback_extra_{idx}",
                hospital_name=f"부산 권역응급의료기관 추가-{idx - base_count + 1}",
                lat=lat,
                lng=lng,
                max_capacity=max_cap,
                current_occupancy=occ,
            )
        )

    return hospitals


def reset_cache() -> None:
    """Clear the in-memory hospital cache (useful between simulation runs)."""
    global _cached_hospitals
    _cached_hospitals = None
