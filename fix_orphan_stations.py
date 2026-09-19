"""
Script pour assigner les gares orphelines a leurs villes parentes.
Phase 1: Assigne les gares aux villes existantes par nom/proximite.
Phase 2: Cree des villes pour les gares orphelines restantes.
"""
import csv
import math
import re
import unicodedata
from collections import defaultdict

STATIONS_CSV = "stations.csv"
OUTPUT_CSV = "stations_fixed.csv"

def normalize_name(s: str) -> str:
    """Normalise un nom pour comparaison (sans accents, minuscules)."""
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    return " ".join(re.sub(r"[^a-z0-9]", " ", s).split())

def extract_city_name(station_name: str) -> str:
    """Extrait le nom de ville d'un nom de gare (ex: 'Paris Gare du Nord' -> 'Paris')."""
    suffixes = [
        r'\s+gare\s+.*$', r'\s+hbf$', r'\s+hauptbahnhof$', r'\s+bahnhof$', r'\s+bf$',
        r'\s+station$', r'\s+central$', r'\s+centrale$', r'\s+termini$',
        r'\s+centre$', r'\s+center$', r'\s+city$', r'\s+ville$',
        r'\s+nord$', r'\s+sud$', r'\s+est$', r'\s+ouest$', r'\s+west$', r'\s+east$',
        r'\s+\(.*\)$', r'\s+-\s+.*$', r'\s+–\s+.*$',
    ]
    name = station_name
    for suffix in suffixes:
        name = re.sub(suffix, '', name, flags=re.IGNORECASE)
    return name.strip()

def haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Distance en km entre deux points GPS."""
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return float('inf')
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def to_float(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None

def make_slug(name: str) -> str:
    """Cree un slug URL-friendly."""
    s = unicodedata.normalize("NFD", name or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")

def main():
    print("[1/5] Lecture de stations.csv...")

    rows = []
    rows_by_id = {}
    cities = []
    orphan_stations = []
    max_id = 0
    fieldnames = None

    with open(STATIONS_CSV, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f, delimiter=';')
        fieldnames = reader.fieldnames

        for row in reader:
            rows.append(row)
            rows_by_id[row['id']] = row

            try:
                row_id = int(row['id'])
                if row_id > max_id:
                    max_id = row_id
            except ValueError:
                pass

            is_city = row.get('is_city', '') == 't'
            parent_id = row.get('parent_station_id', '').strip()
            lat = to_float(row.get('latitude', ''))
            lon = to_float(row.get('longitude', ''))
            country = row.get('country', '')[:2].upper() if row.get('country') else ''

            if is_city:
                cities.append({
                    'id': row['id'],
                    'name': row['name'],
                    'normalized': normalize_name(row['name']),
                    'lat': lat,
                    'lon': lon,
                    'country': country,
                })
            elif not parent_id and lat is not None and lon is not None:
                orphan_stations.append({
                    'row': row,
                    'name': row['name'],
                    'normalized': normalize_name(row['name']),
                    'lat': lat,
                    'lon': lon,
                    'country': country,
                })

    print(f"  -> {len(rows)} entrees chargees")
    print(f"  -> {len(cities)} villes existantes")
    print(f"  -> {len(orphan_stations)} gares orphelines a traiter")
    print(f"  -> ID max: {max_id}")

    # Index des villes par pays
    cities_by_country = defaultdict(list)
    for city in cities:
        cities_by_country[city['country']].append(city)

    assigned = 0
    still_orphan = []

    print("\n[2/5] Attribution aux villes existantes...")

    for i, orphan in enumerate(orphan_stations):
        if (i + 1) % 10000 == 0:
            print(f"   Progression: {i + 1}/{len(orphan_stations)}")

        row = orphan['row']
        best_city = None
        best_dist = float('inf')

        station_name = orphan['normalized']
        first_word = station_name.split()[0] if station_name else ''
        country_cities = cities_by_country.get(orphan['country'], [])

        for city in country_cities:
            if city['normalized'] == station_name:
                dist = haversine_km(orphan['lat'], orphan['lon'], city['lat'], city['lon'])
                if dist < 50:
                    best_city = city
                    best_dist = dist
                    break

            if city['normalized'] and city['normalized'] in station_name:
                dist = haversine_km(orphan['lat'], orphan['lon'], city['lat'], city['lon'])
                if dist < best_dist and dist < 30:
                    best_city = city
                    best_dist = dist

            if first_word and len(first_word) > 2 and city['normalized'].startswith(first_word):
                dist = haversine_km(orphan['lat'], orphan['lon'], city['lat'], city['lon'])
                if dist < best_dist and dist < 20:
                    best_city = city
                    best_dist = dist

        if not best_city:
            for city in country_cities:
                dist = haversine_km(orphan['lat'], orphan['lon'], city['lat'], city['lon'])
                if dist < best_dist and dist < 15:
                    best_city = city
                    best_dist = dist

        if best_city:
            row['parent_station_id'] = best_city['id']
            assigned += 1
        else:
            still_orphan.append(orphan)

    print(f"  -> {assigned} gares assignees a des villes existantes")
    print(f"  -> {len(still_orphan)} gares encore orphelines")

    # Phase 2: Creer des villes pour les gares orphelines
    print("\n[3/5] Creation de villes pour les gares orphelines...")

    new_cities = []
    new_city_id = max_id + 1

    # Grouper les orphelins par nom de ville extrait et pays
    orphan_groups = defaultdict(list)
    for orphan in still_orphan:
        city_name = extract_city_name(orphan['name'])
        key = (normalize_name(city_name), orphan['country'])
        orphan_groups[key].append(orphan)

    print(f"  -> {len(orphan_groups)} groupes de villes potentielles")

    created_cities = 0
    assigned_to_new = 0

    for (normalized_city, country), group in orphan_groups.items():
        if not normalized_city or len(normalized_city) < 2:
            continue

        # Calculer le centroide du groupe
        lats = [o['lat'] for o in group if o['lat']]
        lons = [o['lon'] for o in group if o['lon']]
        if not lats or not lons:
            continue

        center_lat = sum(lats) / len(lats)
        center_lon = sum(lons) / len(lons)

        # Verifier qu'il n'y a pas deja une ville proche
        existing_city = None
        for city in cities_by_country.get(country, []):
            dist = haversine_km(center_lat, center_lon, city['lat'], city['lon'])
            if dist < 10 and (city['normalized'] == normalized_city or normalized_city in city['normalized']):
                existing_city = city
                break

        if existing_city:
            # Assigner a la ville existante trouvee
            for orphan in group:
                orphan['row']['parent_station_id'] = existing_city['id']
                assigned_to_new += 1
        else:
            # Creer une nouvelle ville
            city_name = extract_city_name(group[0]['name'])
            new_city = {field: '' for field in fieldnames}
            new_city['id'] = str(new_city_id)
            new_city['name'] = city_name
            new_city['slug'] = make_slug(city_name)
            new_city['latitude'] = str(center_lat)
            new_city['longitude'] = str(center_lon)
            new_city['country'] = country
            new_city['time_zone'] = group[0]['row'].get('time_zone', '')
            new_city['is_city'] = 't'
            new_city['is_main_station'] = 'f'
            new_city['is_airport'] = 'f'
            new_city['is_suggestable'] = 'f'
            new_city['normalised_code'] = f'urn:trainline:public:nloc:csv{new_city_id}'

            rows.append(new_city)
            new_cities.append({
                'id': str(new_city_id),
                'name': city_name,
                'normalized': normalize_name(city_name),
                'lat': center_lat,
                'lon': center_lon,
                'country': country,
            })
            cities_by_country[country].append(new_cities[-1])

            # Assigner les gares a cette nouvelle ville
            for orphan in group:
                orphan['row']['parent_station_id'] = str(new_city_id)
                assigned_to_new += 1

            new_city_id += 1
            created_cities += 1

    print(f"  -> {created_cities} nouvelles villes creees")
    print(f"  -> {assigned_to_new} gares assignees aux nouvelles villes")

    # Compter les orphelins restants
    remaining_orphans = sum(1 for row in rows if row.get('is_city') != 't' and not row.get('parent_station_id', '').strip())

    print(f"\n[4/5] Resultats finaux:")
    print(f"  -> {assigned + assigned_to_new} gares assignees au total")
    print(f"  -> {remaining_orphans} gares encore orphelines")

    # Ecrire le fichier mis a jour
    print(f"\n[5/5] Ecriture de {OUTPUT_CSV}...")

    with open(OUTPUT_CSV, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=';')
        writer.writeheader()
        writer.writerows(rows)

    print("Termine!")
    print(f"\nPour appliquer les changements:")
    print(f"   mv {OUTPUT_CSV} {STATIONS_CSV}")

if __name__ == "__main__":
    main()
