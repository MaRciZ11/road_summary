import sys
import os
from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout, QPushButton, QFileDialog
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtCore import QUrl
import folium
import geopandas as gpd
from shapely.geometry import LineString
from pyproj import Geod

class MapApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('KML Map Viewer')
        self.resize(800, 600)

        self.layout = QVBoxLayout()
        self.setLayout(self.layout)

        self.button = QPushButton('Wczytaj plik KML')
        self.button.clicked.connect(self.load_kml)
        self.layout.addWidget(self.button)

        self.web_view = QWebEngineView()
        self.layout.addWidget(self.web_view)

    def load_kml(self):
        options = QFileDialog.Options()
        kml_file, _ = QFileDialog.getOpenFileName(self, "Wybierz plik KML", "", "Pliki KML (*.kml);;Wszystkie pliki (*)", options=options)
        if kml_file:
            try:
            
                gdf = gpd.read_file(kml_file, driver='KML')

                if gdf.empty:
                    print("Brak danych w pliku KML.")
                    return

                
                geojson_file = 'data.geojson'
                gdf.to_file(geojson_file, driver='GeoJSON')

                gdf = gpd.read_file(geojson_file)

                # Jeśli geometria jest LineString, podzielmy ją na segmenty
                if gdf.geometry.iloc[0].geom_type == 'LineString':
                    line = gdf.geometry.iloc[0]
                    coords = list(line.coords)

                    # Zakładamy, że czas między kolejnymi punktami to 1 minuta
                    time_per_segment = 1 / 60  # w godzinach (1 minuta)

                    geod = Geod(ellps='WGS84')

                    # Tworzymy listę segmentów z obliczoną prędkością
                    segments = []
                    for i in range(len(coords) - 1):
                        start = coords[i]
                        end = coords[i + 1]

                        # Obliczamy odległość między punktami w metrach
                        azimuth1, azimuth2, distance = geod.inv(start[0], start[1], end[0], end[1])

                        # Obliczamy prędkość w km/h
                        speed = (distance / 1000) / time_per_segment

                        # Tworzymy odcinek
                        segment = {
                            'geometry': LineString([start, end]),
                            'speed': speed
                        }
                        segments.append(segment)

                    # Tworzymy GeoDataFrame z segmentów
                    segments_gdf = gpd.GeoDataFrame(segments, crs="EPSG:4326")

                    # Obliczanie środka mapy
                    centroid = segments_gdf.geometry.centroid.unary_union.centroid
                    center = [centroid.y, centroid.x]

                    # Tworzenie mapy z domyślnym centrum i responsywnym rozmiarem
                    self.map = folium.Map(location=center, zoom_start=10, width='100%', height='100%')

                    # Funkcja stylizująca odcinki na podstawie prędkości
                    def style_function(feature):
                        speed = feature['properties']['speed']
                        # Przypisanie koloru na podstawie prędkości
                        if speed <= 30:
                            color = 'green'
                        elif 30 < speed <= 60:
                            color = 'orange'
                        else:
                            color = 'red'
                        return {
                            'color': color,
                            'weight': 5,
                            'opacity': 0.8
                        }

                    # Funkcja generująca tooltip z prędkością
                    def tooltip_function(feature):
                        speed = feature['properties']['speed']
                        return folium.Tooltip(f"Prędkość: {speed:.2f} km/h")

                    # Dodanie warstwy GeoJSON do mapy z funkcjami stylizującymi
                    folium.GeoJson(
                        segments_gdf,
                        style_function=style_function,
                        tooltip=tooltip_function
                    ).add_to(self.map)

                    # Dopasowanie widoku mapy do granic danych
                    bounds = segments_gdf.total_bounds  # [minx, miny, maxx, maxy]
                    self.map.fit_bounds([[bounds[1], bounds[0]], [bounds[3], bounds[2]]])

                    # Dodanie legendy
                    self.add_legend()

                    # Dodanie stylów CSS dla responsywności
                    self.add_responsive_css()

                    # Zapis mapy do pliku HTML
                    map_file = 'map.html'
                    self.map.save(map_file)

                    # Wyświetlenie mapy w oknie aplikacji
                    self.web_view.load(QUrl.fromLocalFile(os.path.abspath(map_file)))
                else:
                    print("Geometria w pliku KML nie jest typu LineString.")
            except Exception as e:
                print(f"Błąd podczas przetwarzania pliku KML: {e}")

    def add_responsive_css(self):
        # Dodanie stylów CSS do mapy
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
         bottom: 50px; left: 50px; width: 150px; height: 90px;
         background-color: white; z-index:9999; font-size:14px;
         border:2px solid grey;
         ">
         &nbsp;<b>Legenda:</b><br>
         &nbsp;<i style="background:green;color:green;">____</i>&nbsp; ≤ 30 km/h<br>
         &nbsp;<i style="background:orange;color:orange;">____</i>&nbsp; 31-60 km/h<br>
         &nbsp;<i style="background:red;color:red;">____</i>&nbsp; > 60 km/h
         </div>
         """
        self.map.get_root().html.add_child(folium.Element(legend_html))

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = MapApp()
    window.show()
    sys.exit(app.exec_())
