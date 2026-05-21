from __future__ import annotations

import io
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Any

import requests


KML_NAMESPACE = {"kml": "http://www.opengis.net/kml/2.2"}
MAP_MIME_TYPES = {
    "application/vnd.google-earth.kml+xml",
    "application/vnd.google-earth.kmz",
}


def is_map_url(url: str | None) -> bool:
    if not url:
        return False
    return url.lower().split("?", 1)[0].endswith((".kml", ".kmz"))


def is_map_document(document: dict[str, Any]) -> bool:
    content_type = str(document.get("content_type") or "").split(";", 1)[0].strip().lower()
    return (
        document.get("document_kind") == "map"
        or content_type in MAP_MIME_TYPES
        or is_map_url(document.get("url"))
    )


def collect_map_documents(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    map_documents = [document for document in documents if is_map_document(document)]

    def sort_key(document: dict[str, Any]) -> tuple[int, int, str]:
        title = str(document.get("title") or "").lower()
        url = str(document.get("url") or "").lower()
        return (
            0 if "location" in title or "demarcation" in title else 1,
            0 if url.endswith(".kml") else 1,
            title,
        )

    return sorted(map_documents, key=sort_key)


def detect_map_format(url: str, content_type: str | None = None) -> str:
    normalized_type = str(content_type or "").split(";", 1)[0].strip().lower()
    lower_url = url.lower().split("?", 1)[0]
    if normalized_type == "application/vnd.google-earth.kmz" or lower_url.endswith(".kmz"):
        return "kmz"
    return "kml"


def fetch_map_document_payload(url: str) -> dict[str, Any]:
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "")
    return {
        "source_url": url,
        "final_url": response.url,
        "content_type": content_type,
        "map_format": detect_map_format(response.url or url, content_type),
        "body": response.content,
    }


def extract_kml_bytes_from_payload(payload: dict[str, Any]) -> bytes:
    if payload["map_format"] == "kml":
        return payload["body"]

    with zipfile.ZipFile(io.BytesIO(payload["body"])) as archive:
        for name in archive.namelist():
            if name.lower().endswith(".kml"):
                return archive.read(name)

    raise ValueError("KMZ file did not contain a KML document.")


def load_map_document_text(url: str) -> str:
    payload = fetch_map_document_payload(url)
    kml_bytes = extract_kml_bytes_from_payload(payload)
    for encoding in ("utf-8-sig", "utf-8", "utf-16"):
        try:
            return kml_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return kml_bytes.decode("latin-1", errors="replace")


def parse_coordinate_text(text: str) -> list[dict[str, float | None]]:
    points: list[dict[str, float | None]] = []
    for token in text.split():
        parts = [part.strip() for part in token.split(",")]
        if len(parts) < 2:
            continue
        try:
            longitude = float(parts[0])
            latitude = float(parts[1])
        except ValueError:
            continue

        altitude: float | None = None
        if len(parts) >= 3 and parts[2]:
            try:
                altitude = float(parts[2])
            except ValueError:
                altitude = None

        points.append({"lon": longitude, "lat": latitude, "alt": altitude})
    return points


def compute_centroid(points: list[dict[str, float | None]]) -> dict[str, float] | None:
    if not points:
        return None
    return {
        "lat": sum(float(point["lat"]) for point in points) / len(points),
        "lon": sum(float(point["lon"]) for point in points) / len(points),
    }


def flatten_geometry_points(features: list[dict[str, Any]]) -> list[dict[str, float | None]]:
    points: list[dict[str, float | None]] = []
    for feature in features:
        if feature["geometry_type"] == "Polygon":
            for ring in feature.get("rings", []):
                points.extend(ring)
        else:
            points.extend(feature.get("coordinates", []))
    return points


def compute_bounds(points: list[dict[str, float | None]]) -> dict[str, float] | None:
    if not points:
        return None
    latitudes = [float(point["lat"]) for point in points]
    longitudes = [float(point["lon"]) for point in points]
    return {
        "min_lat": min(latitudes),
        "max_lat": max(latitudes),
        "min_lon": min(longitudes),
        "max_lon": max(longitudes),
    }


def build_point_feature(
    *,
    name: str,
    description: str | None,
    coordinates: list[dict[str, float | None]],
) -> dict[str, Any]:
    return {
        "name": name or "Unnamed placemark",
        "description": description,
        "geometry_type": "Point",
        "coordinates": coordinates,
        "coordinate_count": len(coordinates),
        "centroid": compute_centroid(coordinates),
    }


def build_line_feature(
    *,
    name: str,
    description: str | None,
    coordinates: list[dict[str, float | None]],
) -> dict[str, Any]:
    return {
        "name": name or "Unnamed placemark",
        "description": description,
        "geometry_type": "LineString",
        "coordinates": coordinates,
        "coordinate_count": len(coordinates),
        "centroid": compute_centroid(coordinates),
    }


def build_polygon_feature(
    *,
    name: str,
    description: str | None,
    rings: list[list[dict[str, float | None]]],
) -> dict[str, Any]:
    polygon_points = [point for ring in rings for point in ring]
    return {
        "name": name or "Unnamed placemark",
        "description": description,
        "geometry_type": "Polygon",
        "rings": rings,
        "coordinate_count": len(polygon_points),
        "centroid": compute_centroid(polygon_points),
    }


def parse_placemark(placemark: ET.Element) -> list[dict[str, Any]]:
    name = placemark.findtext("kml:name", default="", namespaces=KML_NAMESPACE).strip()
    description = placemark.findtext("kml:description", default="", namespaces=KML_NAMESPACE).strip() or None
    features: list[dict[str, Any]] = []

    for point_element in placemark.findall(".//kml:Point", KML_NAMESPACE):
        coordinate_text = point_element.findtext("kml:coordinates", default="", namespaces=KML_NAMESPACE)
        coordinates = parse_coordinate_text(coordinate_text)
        if coordinates:
            features.append(
                build_point_feature(
                    name=name,
                    description=description,
                    coordinates=coordinates,
                )
            )

    for line_element in placemark.findall(".//kml:LineString", KML_NAMESPACE):
        coordinate_text = line_element.findtext("kml:coordinates", default="", namespaces=KML_NAMESPACE)
        coordinates = parse_coordinate_text(coordinate_text)
        if coordinates:
            features.append(
                build_line_feature(
                    name=name,
                    description=description,
                    coordinates=coordinates,
                )
            )

    for polygon_element in placemark.findall(".//kml:Polygon", KML_NAMESPACE):
        rings: list[list[dict[str, float | None]]] = []
        for ring_element in polygon_element.findall(".//kml:LinearRing", KML_NAMESPACE):
            coordinate_text = ring_element.findtext("kml:coordinates", default="", namespaces=KML_NAMESPACE)
            ring_coordinates = parse_coordinate_text(coordinate_text)
            if ring_coordinates:
                rings.append(ring_coordinates)

        if rings:
            features.append(
                build_polygon_feature(
                    name=name,
                    description=description,
                    rings=rings,
                )
            )

    if features:
        return features

    look_at_lon = placemark.findtext(".//kml:LookAt/kml:longitude", default="", namespaces=KML_NAMESPACE)
    look_at_lat = placemark.findtext(".//kml:LookAt/kml:latitude", default="", namespaces=KML_NAMESPACE)
    try:
        longitude = float(look_at_lon)
        latitude = float(look_at_lat)
    except ValueError:
        return []

    return [
        build_point_feature(
            name=name or "Viewpoint",
            description=description,
            coordinates=[{"lon": longitude, "lat": latitude, "alt": None}],
        )
    ]


def parse_map_document(url: str) -> dict[str, Any]:
    payload = fetch_map_document_payload(url)
    root = ET.fromstring(extract_kml_bytes_from_payload(payload))

    document_name = root.findtext(".//kml:Document/kml:name", default="", namespaces=KML_NAMESPACE).strip()
    features: list[dict[str, Any]] = []
    for placemark in root.findall(".//kml:Placemark", KML_NAMESPACE):
        features.extend(parse_placemark(placemark))

    all_points = flatten_geometry_points(features)
    geometry_counts = Counter(feature["geometry_type"] for feature in features)

    return {
        "source_url": payload["source_url"],
        "final_url": payload["final_url"],
        "content_type": payload["content_type"],
        "map_format": payload["map_format"],
        "document_name": document_name or None,
        "geometry_count": len(features),
        "coordinate_count": len(all_points),
        "feature_counts": dict(geometry_counts),
        "center": compute_centroid(all_points),
        "bounds": compute_bounds(all_points),
        "features": features,
    }
