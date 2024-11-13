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
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from cachetools import LRUCache
from functools import lru_cache

class MapApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('KML Map Viewer with Speed Limits')
        self.resize(800, 600)
        self.speed_limit_cache = LRUCache(maxsize=1000)
        self.setup_ui()
        
    def setup_ui(self):
        self.layout = QVBoxLayout()
        self.setLayout(self.layout)

        # Przycisk wczytywania
        self.button = QPushButton('Wczytaj plik KML')
        self.button.clicked.connect(self.load_kml)
        self.layout.addWidget(self.button)

        # Pasek postępu
        self.progress_label = QLabel('Postęp:')
        self.progress_label.setVisible(False)
        self.layout.addWidget(self.progress_label)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.layout.addWidget(self.progress_bar)

        # Status
        self.status_label = QLabel('')
        self.status_label.setAlignment(Qt.AlignCenter)
        font = QFont()
        font.setPointSize(10)
        self.status_label.setFont(font)
        self.layout.addWidget(self.status_label)

        # Widok mapy
        self.web_view = QWebEngineView()
        self.layout.addWidget(self.web_view)

    @lru_cache(maxsize=1000)
    def get_speed_limit(self, lat, lon):
        """Pobiera limit prędkości z OpenStreetMap dla danej lokalizacji z cache."""
        cache_key = f"{lat:.5f},{lon:.5f}"
        
        if cache_key in self.speed_limit_cache:
            return self.speed_limit_cache[cache_key]

        try:
            overpass_url = "http://overpass-api.de/api/interpreter"
            radius = 20  # Zwiększony promień wyszukiwania
            
            query = f"""
            [out:json];
            (
                way(around:{radius},{lat},{lon})[highway][maxspeed];
                way(around:{radius},{lat},{lon})[highway];
            );
            out body;
            """
            
            response = requests.post(overpass_url, data=query, timeout=5)
            if response.status_code != 200:
                return self.get_default_speed_limit(lat, lon)

            data = response.json()
            
            for element in data.get('elements', []):
                if element.get('type') == 'way':
                    tags = element.get('tags', {})
                    if 'maxspeed' in tags:
                        try:
                            speed_str = tags['maxspeed'].split()[0]
                            speed_limit = int(speed_str)
                            self.speed_limit_cache[cache_key] = speed_limit
                            return speed_limit
                        except (ValueError, IndexError):
                            continue
                    elif 'highway' in tags:
                        # Domyślne limity prędkości na podstawie typu drogi
                        highway_type = tags['highway']
                        default_limits = {
                            'motorway': 140,
                            'trunk': 120,
                            'primary': 90,
                            'secondary': 90,
                            'tertiary': 90,
                            'residential': 50,
                            'service': 30
                        }
                        if highway_type in default_limits:
                            speed_limit = default_limits[highway_type]
                            self.speed_limit_cache[cache_key] = speed_limit
                            return speed_limit
            
            return self.get_default_speed_limit(lat, lon)
            
        except Exception as e:
            print(f"Błąd podczas pobierania limitu prędkości: {e}")
            return self.get_default_speed_limit(lat, lon)

    def get_default_speed_limit(self, lat, lon):
        """Zwraca domyślny limit prędkości na podstawie typu drogi."""
        default_speed = 50  # km/h dla obszaru zabudowanego
        self.speed_limit_cache[f"{lat:.5f},{lon:.5f}"] = default_speed
        return default_speed

    def process_segment(self, segment_data):
        """Przetwarza pojedynczy segment drogi."""
        try:
            segment = segment_data['geometry']
            measured_speed = segment_data['speed']
            mid_point = segment.interpolate(0.5, normalized=True)
            speed_limit = self.get_speed_limit(mid_point.y, mid_point.x)          
            speed_difference = measured_speed - speed_limit
            
            return {
                'geometry': segment,
                'speed': measured_speed,
                'speed_limit': speed_limit,
                'speed_difference': speed_difference
            }
        except Exception as e:
            print(f"Błąd przetwarzania segmentu: {e}")
            return None

    def load_kml(self):
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
                    line = gdf.geometry.iloc[0]
                    coords = list(line.coords)
                    time_per_segment = 1 / 360  # 10 sekund
                    geod = Geod(ellps='WGS84')

                    # Przygotowanie segmentów
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
                    
                    self.progress_bar.setMaximum(len(segments))
                    processed_segments = []
                    
                    batch_size = 10
                    for i in range(0, len(segments), batch_size):
                        batch = segments[i:i + batch_size]
                        with ThreadPoolExecutor(max_workers=4) as executor:
                            futures = [executor.submit(self.process_segment, segment) 
                                     for segment in batch]
                            
                            for future in as_completed(futures):
                                result = future.result()
                                if result:
                                    processed_segments.append(result)
                                self.progress_bar.setValue(len(processed_segments))
                                QApplication.processEvents()

                    self.status_label.setText("Generowanie mapy...")
                    
                    segments_gdf = gpd.GeoDataFrame(processed_segments, crs="EPSG:4326")
                    centroid = segments_gdf.geometry.centroid.unary_union.centroid
                    center = [centroid.y, centroid.x]

                    self.map = folium.Map(location=center, zoom_start=10)

                    def style_function(feature):
                        speed_diff = feature['properties']['speed_difference']
                        if speed_diff <= 10:
                            color = 'green'
                        elif 10 < speed_diff <= 20:
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
                        tooltip=tooltip_function
                    ).add_to(self.map)

                    bounds = segments_gdf.total_bounds
                    self.map.fit_bounds([[bounds[1], bounds[0]], [bounds[3], bounds[2]]])

                    self.add_legend()
                    self.add_responsive_css()

                    map_file = 'map.html'
                    self.map.save(map_file)
                    self.web_view.load(QUrl.fromLocalFile(os.path.abspath(map_file)))
                    
                    self.status_label.setText("Mapa została wygenerowana.")
                else:
                    self.status_label.setText("Geometria w pliku KML nie jest typu LineString.")

            except Exception as e:
                self.status_label.setText(f"Błąd: {str(e)}")
            finally:
                self.button.setEnabled(True)
                self.progress_bar.setVisible(False)
                self.progress_label.setVisible(False)

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
         &nbsp;<i style="background:green;color:green;">____</i>&nbsp; ≤ 10 km/h<br>
         &nbsp;<i style="background:orange;color:orange;">____</i>&nbsp; 10-20 km/h<br>
         &nbsp;<i style="background:red;color:red;">____</i>&nbsp; > 20 km/h
         </div>
         """
        self.map.get_root().html.add_child(folium.Element(legend_html))

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = MapApp()
    window.show()
    sys.exit(app.exec_())