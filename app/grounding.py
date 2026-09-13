from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from difflib import SequenceMatcher
import httpx

from .config import Settings
from .domain import Coordinate, GroundedCandidate, LandmarkPhrase


def haversine_meters(a: Coordinate, b: Coordinate) -> float:
    radius = 6_371_000.0
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = lat2 - lat1
    dlng = math.radians(b.lng - a.lng)
    value = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


@dataclass(slots=True)
class PlaceResult:
    place_id: str | None
    name: str
    formatted_address: str | None
    location: Coordinate
    source: str
    prior_score: float = 0.0


class MapboxGrounder:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def ground_all(
        self, landmarks: list[LandmarkPhrase], center: Coordinate
    ) -> dict[str, list[GroundedCandidate]]:
        groups = await asyncio.gather(*(self.ground(item, center) for item in landmarks))
        return {landmark.normalized_name: result for landmark, result in zip(landmarks, groups)}

    async def ground(
        self, landmark: LandmarkPhrase, center: Coordinate
    ) -> list[GroundedCandidate]:
        places = await self._places(landmark, center)
        candidates: list[GroundedCandidate] = []
        for place in places:
            distance = haversine_meters(center, place.location)
            if distance > self.settings.grounding_radius_meters * 1.5:
                continue
            name_score = SequenceMatcher(
                None, landmark.normalized_name, place.name.lower()
            ).ratio()
            distance_score = max(
                0.0, 1.0 - distance / max(1, self.settings.grounding_radius_meters)
            )
            confidence = min(
                0.99,
                0.50 * name_score
                + 0.30 * distance_score
                + 0.10 * landmark.confidence
                + 0.10 * place.prior_score,
            )
            candidates.append(
                GroundedCandidate(
                    phrase=landmark.normalized_name,
                    place_id=place.place_id,
                    name=place.name,
                    formatted_address=place.formatted_address,
                    location=place.location,
                    distance_meters=round(distance, 1),
                    name_score=round(name_score, 4),
                    distance_score=round(distance_score, 4),
                    prior_score=place.prior_score,
                    confidence=round(confidence, 4),
                    source=place.source,
                )
            )
        candidates.sort(key=lambda item: item.confidence, reverse=True)
        return candidates[:3]

    async def search_address(
        self, query: str, center: Coordinate | None = None
    ) -> list[PlaceResult]:
        """Resolve a typed destination without persisting Mapbox's temporary result."""
        if not self.settings.mapbox_access_token:
            return []
        params = {
            "q": query,
            "access_token": self.settings.mapbox_access_token,
            "language": "en",
            "types": "address,street,neighborhood,locality,place,district",
            "autocomplete": "true",
            "limit": "5",
        }
        if center:
            params["proximity"] = f"{center.lng},{center.lat}"
        landmark = LandmarkPhrase(
            name=query,
            normalized_name=query.lower(),
            landmark_type="address",
            confidence=1.0,
        )
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.get(self.settings.mapbox_geocoding_url, params=params)
            response.raise_for_status()
            return self._mapbox_results(response.json(), landmark, "mapbox_geocoding")

    async def _places(
        self, landmark: LandmarkPhrase, center: Coordinate
    ) -> list[PlaceResult]:
        if self.settings.mapbox_access_token:
            try:
                return await self._mapbox_places(landmark, center)
            except (httpx.HTTPError, KeyError, ValueError):
                if not self.settings.demo_mode:
                    raise
        return self._demo_places(landmark, center)

    async def _mapbox_places(
        self, landmark: LandmarkPhrase, center: Coordinate
    ) -> list[PlaceResult]:
        radius_degrees = self.settings.grounding_radius_meters / 111_320
        longitude_degrees = radius_degrees / max(
            0.2, math.cos(math.radians(center.lat))
        )
        bbox = ",".join(
            str(value)
            for value in (
                center.lng - longitude_degrees,
                center.lat - radius_degrees,
                center.lng + longitude_degrees,
                center.lat + radius_degrees,
            )
        )
        params = {
            "q": landmark.name,
            "access_token": self.settings.mapbox_access_token or "",
            "proximity": f"{center.lng},{center.lat}",
            "country": "NG",
            "types": "poi,address,street,neighborhood,locality,place",
            "language": "en",
            "limit": "5",
            "radius": f"{radius_degrees:.6f}",
        }
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.get(self.settings.mapbox_search_url, params=params)
            response.raise_for_status()
            data = response.json()
            results = self._mapbox_results(data, landmark, "mapbox_search")
            if results:
                return results
            geocoding_params = {
                "q": landmark.name,
                "access_token": self.settings.mapbox_access_token or "",
                "proximity": f"{center.lng},{center.lat}",
                "country": "ng",
                "types": "address,street,neighborhood,locality,place,district",
                "language": "en",
                "limit": "5",
                "bbox": bbox,
            }
            response = await client.get(
                self.settings.mapbox_geocoding_url, params=geocoding_params
            )
            response.raise_for_status()
            return self._mapbox_results(
                response.json(), landmark, "mapbox_geocoding"
            )

    def _mapbox_results(
        self, data: dict, landmark: LandmarkPhrase, source: str
    ) -> list[PlaceResult]:
        results: list[PlaceResult] = []
        for feature in data.get("features", []):
            coordinates = (feature.get("geometry") or {}).get("coordinates") or []
            if len(coordinates) < 2:
                continue
            properties = feature.get("properties") or {}
            results.append(
                PlaceResult(
                    place_id=properties.get("mapbox_id") or feature.get("id"),
                    name=(
                        properties.get("name_preferred")
                        or properties.get("name")
                        or landmark.name
                    ),
                    formatted_address=(
                        properties.get("full_address")
                        or properties.get("place_formatted")
                        or feature.get("place_name")
                    ),
                    location=Coordinate(lat=coordinates[1], lng=coordinates[0]),
                    source=source,
                )
            )
        return results

    def _demo_places(self, landmark: LandmarkPhrase, center: Coordinate) -> list[PlaceResult]:
        offsets = {
            "mobil": (0.0006, -0.0004),
            "mobil filling station": (0.0006, -0.0004),
            "mosque": (0.0010, 0.0005),
            "black gate": (0.0011, 0.0004),
            "firstbank": (-0.0003, 0.0008),
            "transformer": (0.0004, 0.0007),
        }
        dlat, dlng = offsets.get(landmark.normalized_name, (0.0005, 0.0005))
        canonical = {
            "mobil": "Mobil Filling Station",
            "mobil filling station": "Mobil Filling Station",
            "mosque": "Community Mosque",
            "black gate": "Black gate",
            "firstbank": "FirstBank",
        }.get(landmark.normalized_name, landmark.name)
        return [
            PlaceResult(
                place_id=f"demo:{landmark.normalized_name.replace(' ', '-')}",
                name=canonical,
                formatted_address="Yaba, Lagos (demo candidate)",
                location=Coordinate(lat=center.lat + dlat, lng=center.lng + dlng),
                source="demo_places",
                prior_score=0.2,
            )
        ]
