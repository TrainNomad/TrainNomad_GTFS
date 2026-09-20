import io
from pathlib import Path
import sys
import unicodedata
import pandas as pd
import reverse_geocoder as rg
from timezonefinder import TimezoneFinder


def remove_accents(text: str) -> str:
  if pd.isna(text) or not text:
    return ''
  nfkd_form = unicodedata.normalize('NFD', str(text))
  return ''.join([c for c in nfkd_form if unicodedata.category(c) != 'Mn'])


def slugify(text: str) -> str:
  clean_text = remove_accents(text)
  return (
      pd.Series([clean_text])
      .str.lower()
      .str.replace(r'[^a-z0-9]+', '-', regex=True)
      .str.strip('-')
      .iloc[0]
  )


# Dictionnaire de traduction/normalisation des noms de districts
CITY_NAME_MAP = {
    'Lisbon': 'Lisboa',
    'Oporto': 'Porto',
}

if __name__ == '__main__':
  tf = TimezoneFinder()
  SCRIPT_DIR = Path(__file__).parent
  stops_path = SCRIPT_DIR / 'stops.txt'

  # 1. Chargement du fichier GTFS stops.txt
  gtfs_stops = pd.read_csv(stops_path, dtype=str)

  if 'location_type' in gtfs_stops.columns:
    gtfs_stops['location_type_clean'] = (
        gtfs_stops['location_type'].fillna('0').astype(str)
    )
    gtfs_stops = gtfs_stops[gtfs_stops['location_type_clean'].isin(['0', '1'])]

  # 2. Extraction des coordonnées GPS
  lats = pd.to_numeric(gtfs_stops['stop_lat'], errors='coerce')
  lons = pd.to_numeric(gtfs_stops['stop_lon'], errors='coerce')
  coords = list(zip(lats, lons))

  # 3. Géocodage au niveau LARGE (admin1)
  sys.stdout = io.StringIO()
  rg_results = rg.search(coords)
  sys.stdout = sys.__stdout__

  city_names = []
  countries = []
  timezones = []

  for i, res in enumerate(rg_results):
    # admin1 extrait la grande ville / district (ex: Lisboa, Porto)
    raw_admin1 = res.get('admin1') or res.get('admin2') or 'Desconhecido'
    clean_city = CITY_NAME_MAP.get(raw_admin1, raw_admin1)

    city_names.append(clean_city)
    countries.append(res['cc'].upper())

    lat, lon = coords[i]
    timezones.append(tf.timezone_at(lat=lat, lng=lon) or 'Europe/Lisbon')

  gtfs_stops['extracted_city'] = city_names
  gtfs_stops['extracted_country'] = countries
  gtfs_stops['extracted_tz'] = timezones

  # 4. Assemblage de la table Trainline (Villes + Gares)
  final_rows = []
  city_id_mapping = {}
  current_id = 1

  # A. Génération des VILLES PRINCIPALES (is_city = True)
  unique_cities = gtfs_stops.groupby('extracted_city').first().reset_index()

  for _, city_row in unique_cities.iterrows():
    c_name = city_row['extracted_city']
    c_slug = slugify(f'city-{c_name}')

    city_id_mapping[c_name] = current_id

    final_rows.append({
        'id': current_id,
        'name': c_name,
        'slug': c_slug,
        'uic': None,
        'uic8_sncf': None,
        'latitude': city_row['stop_lat'],
        'longitude': city_row['stop_lon'],
        'parent_station_id': None,
        'country': city_row['extracted_country'],
        'time_zone': city_row['extracted_tz'],
        'is_city': True,
        'is_main_station': False,
        'is_airport': False,
        'cp_id': None,
    })
    current_id += 1

  # B. Génération des GARES (is_city = False) rattachées à la ville parent
  for idx, stop_row in gtfs_stops.iterrows():
    stop_name = (
        str(stop_row['stop_name']) if pd.notna(stop_row['stop_name']) else ''
    )
    city_name = stop_row['extracted_city']
    parent_city_id = city_id_mapping.get(city_name)

    stop_slug = slugify(stop_name)
    uic_cleaned = str(stop_row['stop_id']).replace('_', '')
    is_airport = 'aeroporto' in stop_name.lower()

    final_rows.append({
        'id': current_id,
        'name': stop_name,
        'slug': stop_slug,
        'uic': uic_cleaned,
        'uic8_sncf': None,
        'latitude': stop_row['stop_lat'],
        'longitude': stop_row['stop_lon'],
        'parent_station_id': parent_city_id,  # Rattaché à la grande ville
        'country': stop_row['extracted_country'],
        'time_zone': stop_row['extracted_tz'],
        'is_city': False,
        'is_main_station': False,
        'is_airport': is_airport,
        'cp_id': stop_row['stop_id'],
    })
    current_id += 1

  # 5. Exportation CSV
  trainline_df = pd.DataFrame(final_rows)
  output_path = SCRIPT_DIR / 'stations_cp_trainline.csv'
  trainline_df.to_csv(output_path, index=False, encoding='utf-8')

  print(
      f'Fichier {output_path.name} généré avec succès ! '
      f'({len(unique_cities)} grandes villes et {len(gtfs_stops)} gares)'
  )