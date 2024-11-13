import sys
import os
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QPushButton, 
                            QFileDialog, QProgressBar, QLabel)
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtCore import QUrl, Qt
from PyQt5.QtGui import QFont
import folium
import geopandas as gpd
from shapely.geometry import LineString, Point
from pyproj import Geod
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from cachetools import LRUCache
from functools import lru_cache
import numpy as np
from rtree import index

class MapApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('KML Map Viewer with Speed Limits')
        self.resize(800, 600)
        self.speed_limit_cache = LRUCache(maxsize=1000)
        self.spatial_index = None
        self.road_segments = None
        self.setup_ui()
        
    def setup_ui(self):
        self.layout = QVBoxLayout()
        self.setLayout(self.layout)

        self.button = QPushButton('Wczytaj plik KML')
        self.button.clicked.connect(self.load_kml)
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

    def create_spatial_index(self, segments):
        idx = index.Index()
        road_segments = []
        
        for i, segment in enumerate(segments):
            idx.insert(i, segment.geometry.bounds)
            road_segments.append(segment)
            
        return idx, road_segments

    def batch_speed_limits_query(self, points, batch_size=50):
        unique_points = list(set((round(p[1], 5), round(p[0], 5)) for p in points))
        results = {}
        
        for i in range(0, len(unique_points), batch_size):
            batch = unique_points[i:i + batch_size]
            
            query_areas = ');('.join(f'{lat},{lon},{lat},{lon}' for lat, lon in batch)
            overpass_url = "http://overpass-api.de/api/interpreter"
            
            query = f"""
            [out:json];
            (
                way(area:3600000000)(
                    {query_areas}
                )[highway][maxspeed];
                way(area:3600000000)(
                    {query_areas}
                )[highway];
            );
            out body;
            """
            
            try:
                response = requests.post(overpass_url, data=query, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    
                    for element in data.get('elements', []):
                        if element.get('type') == 'way':
                            tags = element.get('tags', {})
                            speed_limit = self.extract_speed_limit(tags)
                            if speed_limit:
                                for lat, lon in batch:
                                    if (lat, lon) not in results:
                                        results[(lat, lon)] = speed_limit
                
            except Exception as e:
                print(f"Błąd zapytania batch: {e}")
                
        for point in unique_points:
            if point not in results:
                results[point] = self.get_default_speed_limit(*point)
                
        return results

    def extract_speed_limit(self, tags):
        if 'maxspeed' in tags:
            try:
                speed_str = tags['maxspeed'].split()[0]
                return int(speed_str)
            except (ValueError, IndexError):
                pass
                
        if 'highway' in tags:
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
            return default_limits.get(highway_type)
        
        return None

    def get_default_speed_limit(self, lat, lon):
        default_speed = 50
        cache_key = f"{lat:.5f},{lon:.5f}"
        self.speed_limit_cache[cache_key] = default_speed
        return default_speed

    def process_segments_batch(self, segments_batch):
        mid_points = [segment['geometry'].interpolate(0.5, normalized=True) 
                     for segment in segments_batch]
        
        speed_limits = self.batch_speed_limits_query(
            [(p.y, p.x) for p in mid_points]
        )
        
        results = []
        for segment, mid_point in zip(segments_batch, mid_points):
            try:
                speed_limit = speed_limits.get(
                    (round(mid_point.y, 5), round(mid_point.x, 5)),
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
                    segments = self.prepare_segments(gdf.geometry.iloc[0])
                    
                    self.progress_bar.setMaximum(len(segments))
                    processed_segments = []
                    
                    batch_size = 50
                    for i in range(0, len(segments), batch_size):
                        batch = segments[i:i + batch_size]
                        results = self.process_segments_batch(batch)
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