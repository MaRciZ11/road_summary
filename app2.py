import sys
import os
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QPushButton, 
                            QFileDialog, QProgressBar, QLabel)
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtCore import QUrl, Qt
from PyQt5.QtGui import QFont
import folium
import geopandas as gpd
from shapely.geometry import LineString
from pyproj import Geod
import aiohttp
import asyncio
from cachetools import LRUCache

class MapApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('KML Map Viewer with Speed Limits')
        self.resize(800, 600)
        self.speed_limit_cache = LRUCache(maxsize=1000)
        self.setup_ui()
        self.loop = asyncio.get_event_loop()
        
    def setup_ui(self):
        self.layout = QVBoxLayout()
        self.setLayout(self.layout)

        self.button = QPushButton('Wczytaj plik KML')
        self.button.clicked.connect(lambda: self.loop.run_until_complete(self.load_kml()))
        self.layout.addWidget(self.button)

        self.progress_label = QLabel('Postęp:')
        self.progress_label.setVisible(False)
        self.layout.addWidget(self.progress_label)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.layout.addWidget(self.progress_bar)

        self.status_label = QLabel('')
        self.status_label.setAlignment(Qt.AlignCenter)
        font = QFont()
        font.setPointSize(10)
        self.status_label.setFont(font)
        self.layout.addWidget(self.status_label)

        self.web_view = QWebEngineView()
        self.layout.addWidget(self.web_view)

    async def batch_speed_limits_query(self, points, batch_size=10):
        unique_points = list(set((round(p.y, 6), round(p.x, 6)) for p in points))
        results = {}
        overpass_url = "http://overpass-api.de/api/interpreter"
        
        # Definiujemy priorytety dla typów dróg
        highway_priority = {
            'motorway': 1,
            'trunk': 2,
            'primary': 3,
            'secondary': 4,
            'tertiary': 5,
            'unclassified': 6,
            'residential': 7,
            'service': 8,
            'living_street': 9,
            'road': 10
        }

        async with aiohttp.ClientSession() as session:
            for i in range(0, len(unique_points), batch_size):
                batch = unique_points[i:i + batch_size]
                
                queries = []
                for lat, lon in batch:
                    # Filtrowanie dróg tylko do głównych typów
                    queries.append(f"way(around:20,{lat},{lon})[highway~'^(motorway|trunk|primary|secondary|tertiary|unclassified|residential)$'];")
                query = f"""
                [out:json][timeout:25];
                (
                    {"".join(queries)}
                );
                out body center tags;
                """
                try:
                    async with session.post(overpass_url, data={'data': query}, timeout=25) as response:
                        if response.status == 200:
                            data = await response.json()
                            elements = data.get('elements', [])
                            
                            for lat, lon in batch:
                                min_distance = None
                                best_priority = None
                                speed_limit = None
                                for element in elements:
                                    if element.get('type') == 'way' and 'highway' in element.get('tags', {}):
                                        tags = element['tags']
                                        highway_type = tags.get('highway')
                                        if highway_type not in highway_priority:
                                            continue  # Pomijamy nieznane typy dróg

                                        if 'center' in element:
                                            way_lat = element['center']['lat']
                                            way_lon = element['center']['lon']
                                            dist = ((lat - way_lat)**2 + (lon - way_lon)**2)
                                            
                                            current_priority = highway_priority[highway_type]
                                            if (best_priority is None or current_priority < best_priority or
                                                (current_priority == best_priority and (min_distance is None or dist < min_distance))):
                                                min_distance = dist
                                                best_priority = current_priority
                                                speed_limit_candidate = self.extract_speed_limit(tags)
                                                if speed_limit_candidate:
                                                    speed_limit = speed_limit_candidate
                                                else:
                                                    speed_limit = self.get_default_speed_limit_by_highway_type(highway_type)
                                if speed_limit:
                                    results[(lat, lon)] = speed_limit
                                else:
                                    results[(lat, lon)] = self.get_default_speed_limit(lat, lon)
                        else:
                            print(f"Response status: {response.status}")
                except Exception as e:
                    print(f"Błąd zapytania batch: {e}")
                    
        return results

    def extract_speed_limit(self, tags):
        if 'maxspeed' in tags:
            speed_str = tags['maxspeed']
            try:
                # Usuwamy wszelkie jednostki i dodatkowe teksty
                speed_value = ''.join(filter(str.isdigit, speed_str))
                if speed_value:
                    return int(speed_value)
            except (ValueError, IndexError):
                pass
        return None

    def get_default_speed_limit_by_highway_type(self, highway_type):
        default_limits = {
            'motorway': 140,
            'trunk': 100,
            'primary': 90,
            'secondary': 90,
            'tertiary': 90,
            'unclassified': 70,
            'residential': 50,
            'living_street': 20,
            'service': 30,
            'road': 50
        }
        return default_limits.get(highway_type, 50)

    def get_default_speed_limit(self, lat, lon):
        default_speed = 50
        cache_key = f"{lat:.6f},{lon:.6f}"
        self.speed_limit_cache[cache_key] = default_speed
        return default_speed

    async def process_segments_batch(self, segments_batch):
        mid_points = [segment['geometry'].interpolate(0.5, normalized=True) 
                      for segment in segments_batch]
        
        speed_limits = await self.batch_speed_limits_query(mid_points)
        
        results = []
        for segment, mid_point in zip(segments_batch, mid_points):
            try:
                speed_limit = speed_limits.get(
                    (round(mid_point.y, 6), round(mid_point.x, 6)),
                    self.get_default_speed_limit(mid_point.y, mid_point.x)
                )
                
                results.append({
                    'geometry': segment['geometry'],
                    'speed': segment['speed'],
                    'speed_limit': speed_limit,
                    'speed_difference': segment['speed'] - speed_limit
                })
            except Exception as e:
                print(f"Błąd przetwarzania segmentu w partii: {e}")
                
        return results

    async def load_kml(self):
        options = QFileDialog.Options()
        kml_file, _ = QFileDialog.getOpenFileName(self, "Wybierz plik KML", "", 
                                                  "Pliki KML (*.kml);;Wszystkie pliki (*)", options=options)
        if kml_file:
            try:
                self.button.setEnabled(False)
                self.progress_bar.setVisible(True)
                self.progress_label.setVisible(True)
                self.status_label.setText("Wczytywanie pliku KML...")
                
                gdf = gpd.read_file(kml_file, driver='KML')

                if gdf.empty:
                    self.status_label.setText("Brak danych w pliku KML.")
                    return

                self.status_label.setText("Przetwarzanie danych...")
                
                if gdf.geometry.iloc[0].geom_type == 'LineString':
                    segments = self.prepare_segments(gdf.geometry.iloc[0])
                    
                    self.progress_bar.setMaximum(len(segments))
                    processed_segments = []
                    
                    batch_size = 10  # Mniejsza partia, aby uniknąć przeciążenia API
                    for i in range(0, len(segments), batch_size):
                        batch = segments[i:i + batch_size]
                        results = await self.process_segments_batch(batch)
                        processed_segments.extend(results)
                        self.progress_bar.setValue(len(processed_segments))
                        QApplication.processEvents()

                    self.generate_map(processed_segments)
                    
                else:
                    self.status_label.setText("Geometria w pliku KML nie jest typu LineString.")

            except Exception as e:
                self.status_label.setText(f"Błąd: {str(e)}")
            finally:
                self.button.setEnabled(True)
                self.progress_bar.setVisible(False)
                self.progress_label.setVisible(False)

    def prepare_segments(self, line):
        coords = list(line.coords)
        time_per_segment = 1 / 360
        geod = Geod(ellps='WGS84')
        
        segments = []
        for i in range(len(coords) - 1):
            start = coords[i]
            end = coords[i + 1]
            
            azimuth1, azimuth2, distance = geod.inv(start[0], start[1], end[0], end[1])
            speed = (distance / 1000) / time_per_segment
            
            segments.append({
                'geometry': LineString([start, end]),
                'speed': speed
            })
            
        return segments

    def generate_map(self, processed_segments):
        self.status_label.setText("Generowanie mapy...")
        
        segments_gdf = gpd.GeoDataFrame(processed_segments, crs="EPSG:4326")
        centroid = segments_gdf.geometry.centroid.unary_union.centroid
        center = [centroid.y, centroid.x]

        self.map = folium.Map(location=center, zoom_start=10)

        def style_function(feature):
            speed = feature['properties']['speed']
            speed_limit = feature['properties']['speed_limit']
            if speed <= speed_limit:
                color = 'green'
            elif speed <= speed_limit + 10:
                color = 'orange'
            else:
                color = 'red'
            return {'color': color, 'weight': 5, 'opacity': 0.8}

        def tooltip_function(feature):
            speed = feature['properties']['speed']
            limit = feature['properties']['speed_limit']
            diff = feature['properties']['speed_difference']
            return folium.Tooltip(
                f"Prędkość: {speed:.1f} km/h<br>"
                f"Limit: {limit} km/h<br>"
                f"Przekroczenie: {diff:.1f} km/h"
            )

        folium.GeoJson(
            segments_gdf,
            style_function=style_function,
            tooltip=tooltip_function,
            highlight_function=lambda x: {'weight': 8, 'color': 'blue'},
            popup=folium.GeoJsonPopup(fields=['speed', 'speed_limit'], aliases=['Prędkość:', 'Limit prędkości:'])
        ).add_to(self.map)

        bounds = segments_gdf.total_bounds
        self.map.fit_bounds([[bounds[1], bounds[0]], [bounds[3], bounds[2]]])

        self.add_legend()
        self.add_responsive_css()

        map_file = 'map.html'
        self.map.save(map_file)
        self.web_view.load(QUrl.fromLocalFile(os.path.abspath(map_file)))
        
        self.status_label.setText("Mapa została wygenerowana.")

    def add_responsive_css(self):
        css = """
        <style>
            html, body, #map {
                width: 100%;
                height: 100%;
                margin: 0;
                padding: 0;
            }
        </style>
        """
        self.map.get_root().html.add_child(folium.Element(css))

    def add_legend(self):
        legend_html = """
         <div style="
         position: fixed;
         bottom: 50px; left: 50px; width: 200px; height: 90px;
         background-color: white; z-index:9999; font-size:14px;
         border:2px solid grey;
         ">
         &nbsp;<b>Przekroczenie prędkości:</b><br>
         &nbsp;<i style="background:green;color:green;">____</i>&nbsp; Prędkość ≤ Limit<br>
         &nbsp;<i style="background:orange;color:orange;">____</i>&nbsp; Prędkość ≤ Limit + 10 km/h<br>
         &nbsp;<i style="background:red;color:red;">____</i>&nbsp; Prędkość > Limit + 10 km/h
         </div>
         """
        self.map.get_root().html.add_child(folium.Element(legend_html))

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = MapApp()
    window.show()
    sys.exit(app.exec_())
