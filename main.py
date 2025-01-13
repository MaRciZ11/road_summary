import sys
import os
import json
import uuid
from PyQt5.QtWidgets import (QApplication, QWidget, QPushButton, 
                             QFileDialog, QProgressBar, QLabel, QHBoxLayout, 
                             QMainWindow, QAction, QVBoxLayout, QToolBar, QTreeWidget, QTreeWidgetItem, 
                             QLineEdit, QDialog, QDialogButtonBox, QComboBox, 
                             QFormLayout, QMessageBox, QScrollArea, QFrame, QGridLayout, QStackedWidget)
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtCore import QUrl, Qt
from PyQt5.QtGui import QFont, QIcon, QPixmap, QPainter, QPainterPath
import folium
import geopandas as gpd, fiona
from shapely.geometry import LineString
from pyproj import Geod
import aiohttp
import asyncio
from cachetools import LRUCache
from lxml import etree

os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
fiona.drvsupport.supported_drivers['kml'] = 'rw'
fiona.drvsupport.supported_drivers['KML'] = 'rw'
fiona.drvsupport.supported_drivers['libkml'] = 'rw'
fiona.drvsupport.supported_drivers['LIBKML'] = 'rw'

KML_NAMESPACE = "http://www.opengis.net/kml/2.2"
NS = {"k": KML_NAMESPACE}

def load_profiles(profiles_file):
    if not os.path.exists(profiles_file):
        data = {"profiles": []}
        with open(profiles_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        return data
    else:
        with open(profiles_file, 'r', encoding='utf-8') as f:
            return json.load(f)

def save_profiles(profiles_file, data):
    with open(profiles_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def parse_segments_from_extended_data(kml_path):
    parser = etree.XMLParser(remove_blank_text=False)
    with open(kml_path, 'rb') as f:
        tree = etree.parse(f, parser)

    placemarks = tree.findall('.//k:Placemark', NS)
    if not placemarks:
        return []
    placemark = placemarks[0]

    extended = placemark.find('.//k:ExtendedData', NS)
    if extended is None:
        return []

    data_elems = extended.findall('.//k:Data', NS)
    segments_dict = {}
    for d in data_elems:
        name = d.get('name')
        if name is None:
            continue
        val_elem = d.find('.//k:value', NS)
        if val_elem is None:
            continue
        val = val_elem.text
        if 'speed_difference_' in name:
            idx = int(name.split('_')[-1])
            segments_dict.setdefault(idx, {})['speed_difference'] = float(val)
        elif 'speed_limit_' in name:
            idx = int(name.split('_')[-1])
            segments_dict.setdefault(idx, {})['speed_limit'] = float(val)
        elif 'speed_' in name and 'speed_limit' not in name and 'difference' not in name:
            idx = int(name.split('_')[-1])
            segments_dict.setdefault(idx, {})['speed'] = float(val)

    if not segments_dict:
        return []

    coords_elem = placemark.find('.//k:LineString/k:coordinates', NS)
    if coords_elem is None:
        return []

    coords_text = coords_elem.text.strip()
    coords_list = coords_text.split()
    points = []
    for c in coords_list:
        lon, lat, alt = c.split(',')
        points.append((float(lon), float(lat)))

    processed_segments = []
    for i in sorted(segments_dict.keys()):
        seg = segments_dict[i]
        if i < len(points)-1:
            seg_geom = LineString([points[i], points[i+1]])
            seg['geometry'] = seg_geom
        else:
            seg['geometry'] = None
        processed_segments.append(seg)
    return processed_segments

def add_extended_data_to_kml(original_kml_path, processed_segments):
    parser = etree.XMLParser(remove_blank_text=False)
    with open(original_kml_path, 'rb') as f:
        tree = etree.parse(f, parser)

    placemarks = tree.findall('.//k:Placemark', NS)
    if not placemarks:
        return
    placemark = placemarks[0]

    extended = placemark.find('.//k:ExtendedData', NS)
    if extended is None:
        extended = etree.SubElement(placemark, '{%s}ExtendedData' % KML_NAMESPACE)

    for i, seg in enumerate(processed_segments):
        speed = seg['speed']
        slimit = seg['speed_limit']
        sdiff = seg['speed_difference']

        data_speed = etree.SubElement(extended, '{%s}Data' % KML_NAMESPACE, name=f"speed_{i}")
        value_speed = etree.SubElement(data_speed, '{%s}value' % KML_NAMESPACE)
        value_speed.text = str(speed)

        data_slimit = etree.SubElement(extended, '{%s}Data' % KML_NAMESPACE, name=f"speed_limit_{i}")
        value_slimit = etree.SubElement(data_slimit, '{%s}value' % KML_NAMESPACE)
        value_slimit.text = str(slimit)

        data_sdiff = etree.SubElement(extended, '{%s}Data' % KML_NAMESPACE, name=f"speed_difference_{i}")
        value_sdiff = etree.SubElement(data_sdiff, '{%s}value' % KML_NAMESPACE)
        value_sdiff.text = str(sdiff)

    with open(original_kml_path, 'wb') as f:
        f.write(etree.tostring(tree, encoding='utf-8', xml_declaration=True))


class ProfileDialog(QDialog):
    def __init__(self, profiles_data, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Wybierz profil lub utwórz nowy")
        self.profiles_data = profiles_data
        self.selected_profile = None
        self.new_profile_name = ""
        self.new_profile_image = ""
        self.init_ui()

    def init_ui(self):
        layout = QFormLayout()
        self.combo = QComboBox()
        self.combo.addItem("Utwórz nowy profil")
        for p in self.profiles_data["profiles"]:
            self.combo.addItem(p["name"])
        layout.addRow("Profil:", self.combo)

        self.name_edit = QLineEdit()
        layout.addRow("Imię nowego profilu:", self.name_edit)

        self.image_button = QPushButton("Wybierz obrazek")
        self.image_button.clicked.connect(self.choose_image)
        layout.addRow("Obrazek profilu:", self.image_button)

        self.button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.button_box.accepted.connect(self.accept_data)
        self.button_box.rejected.connect(self.reject)
        layout.addRow(self.button_box)

        self.setLayout(layout)
        self.combo.currentIndexChanged.connect(self.combo_changed)
        self.combo_changed(self.combo.currentIndex())

    def combo_changed(self, index):
        if index == 0:
            self.name_edit.setEnabled(True)
            self.image_button.setEnabled(True)
        else:
            self.name_edit.setEnabled(False)
            self.image_button.setEnabled(False)

    def choose_image(self):
        img, _ = QFileDialog.getOpenFileName(self, "Wybierz obrazek", "", "Obrazy (*.png *.jpg *.jpeg *.bmp)")
        if img:
            self.new_profile_image = img

    def accept_data(self):
        idx = self.combo.currentIndex()
        if idx == 0:
            name = self.name_edit.text().strip()
            if not name:
                QMessageBox.warning(self, "Błąd", "Musisz podać imię profilu.")
                return
            self.new_profile_name = name
            self.selected_profile = None
        else:
            self.selected_profile = self.combo.currentText()
        self.accept()


class DragDropTree(QTreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QTreeWidget.InternalMove)
        self.setSelectionMode(QTreeWidget.ExtendedSelection)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat('application/x-qabstractitemmodeldatalist'):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        item = self.itemAt(event.pos())
        if item is not None:
            if item.parent() is None:
                event.acceptProposedAction()
            else:
                event.ignore()
        else:
            event.ignore()

    def dropEvent(self, event):
        source_item = None
        if event.source() == self:
            source_item = self.currentItem()
        super().dropEvent(event)
        # Po drop aktualizujemy JSON
        parent = None
        if source_item is not None:
            new_parent = source_item.parent()
            if new_parent is not None:
                kml_file = source_item.data(0, Qt.UserRole)
                if kml_file:
                    app = self.window()
                    if hasattr(app, 'profiles_data'):
                        old_profile_name = None
                        for p in app.profiles_data["profiles"]:
                            if kml_file in p["rides"]:
                                old_profile_name = p["name"]
                                break
                        new_profile_name = new_parent.text(0)

                        if old_profile_name and new_profile_name and old_profile_name != new_profile_name:
                            for p in app.profiles_data["profiles"]:
                                if p["name"] == old_profile_name:
                                    p["rides"].remove(kml_file)
                                if p["name"] == new_profile_name:
                                    if kml_file not in p["rides"]:
                                        p["rides"].append(kml_file)
                            save_profiles(app.profiles_file, app.profiles_data)
                            app.build_profile_tree()


class MapApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Speed Limits')
        self.resize(1024, 768)
        self.speed_limit_cache = LRUCache(maxsize=1000)
        self.maps_dir = 'maps'
        if not os.path.exists(self.maps_dir):
            os.makedirs(self.maps_dir)
        
        self.profiles_file = 'profiles.json'
        self.profiles_data = load_profiles(self.profiles_file)
        self.map_files = {}

        self.maps_dir = 'maps'
        if not os.path.exists(self.maps_dir):
            os.makedirs(self.maps_dir)

        self.loop = asyncio.get_event_loop()

        # StackedWidget do przełączania widoków
        self.stacked = QStackedWidget()

        # Widok przejazdów
        self.rides_widget = QWidget()
        self.setup_rides_ui(self.rides_widget)

        # Widok profili
        self.profiles_widget = QWidget()
        self.setup_profiles_ui(self.profiles_widget)

        # Dodajemy do stosu
        self.stacked.addWidget(self.rides_widget)
        self.stacked.addWidget(self.profiles_widget)

        self.setCentralWidget(self.stacked)

        self.create_toolbar()

        # Domyślny widok: przejazdy
        self.show_rides_view()

        self.build_profile_tree()
        self.update_profiles_view()

    def create_toolbar(self):
        self.toolbar = QToolBar("Main Toolbar")
        self.addToolBar(self.toolbar)
        
        logo_action = QAction(QIcon('logo.png'), 'Logo', self)
        self.toolbar.addAction(logo_action)

        profile_action = QAction("Profile", self)
        profile_action.triggered.connect(self.show_profiles_view)
        self.toolbar.addAction(profile_action)

        rides_action = QAction("Przejazdy", self)
        rides_action.triggered.connect(self.show_rides_view)
        self.toolbar.addAction(rides_action)
        
        close_action = QAction('Zamknij', self)
        close_action.triggered.connect(self.close)
        self.toolbar.addAction(close_action)

    def show_profiles_view(self):
        self.stacked.setCurrentIndex(1)

    def show_rides_view(self):
        self.stacked.setCurrentIndex(0)

    def setup_rides_ui(self, parent_widget):
        layout = QVBoxLayout(parent_widget)
        top_layout = QHBoxLayout()

        self.web_view = QWebEngineView()
        top_layout.addWidget(self.web_view)

        self.side_panel = QWidget()
        self.side_layout = QVBoxLayout()
        self.side_panel.setLayout(self.side_layout)
        top_layout.addWidget(self.side_panel)

        layout.addLayout(top_layout)

        self.load_button = QPushButton('Wczytaj plik KML')
        self.load_button.setStyleSheet("padding: 10px; font-size: 16px;")
        self.load_button.clicked.connect(lambda: self.loop.run_until_complete(self.load_kml()))
        self.side_layout.addWidget(self.load_button)

        self.profile_tree = DragDropTree()
        self.profile_tree.itemClicked.connect(self.tree_item_clicked)
        self.side_layout.addWidget(self.profile_tree)

        self.remove_button = QPushButton("Usuń zaznaczone")
        self.remove_button.clicked.connect(self.remove_selected_items)
        self.side_layout.addWidget(self.remove_button)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.side_layout.addWidget(self.progress_bar)

        self.status_label = QLabel('')
        self.status_label.setAlignment(Qt.AlignCenter)
        font = QFont()
        font.setPointSize(10)
        self.status_label.setFont(font)
        self.side_layout.addWidget(self.status_label)

        self.initialize_map()

    def setup_profiles_ui(self, parent_widget):
        layout = QVBoxLayout(parent_widget)

        # Scrollable area with grid of profiles
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.profiles_container = QWidget()
        self.profiles_layout = QGridLayout(self.profiles_container)
        self.profiles_layout.setSpacing(20)
        self.profiles_layout.setContentsMargins(20,20,20,20)
        scroll.setWidget(self.profiles_container)
        layout.addWidget(scroll)

    def update_profiles_view(self):
        # Usuwamy stare kafelki
        for i in reversed(range(self.profiles_layout.count())):
            widget = self.profiles_layout.itemAt(i).widget()
            if widget:
                widget.setParent(None)

        # Dodajemy kafelki dla każdego profilu
        row = 0
        col = 0
        for p in self.profiles_data["profiles"]:
            w = self.create_profile_card(p)
            self.profiles_layout.addWidget(w, row, col)
            col += 1
            if col > 2:
                col = 0
                row += 1

    def create_profile_card(self, profile):
        # Wyliczamy statystyki: liczba przejazdów, ocena
        num_rides = len(profile["rides"])
        avg_penalty = self.calculate_profile_penalty(profile)
        grade = self.penalty_to_grade(avg_penalty)

        card = QFrame()
        card.setFrameShape(QFrame.StyledPanel)
        card_layout = QVBoxLayout(card)
        card_layout.setAlignment(Qt.AlignCenter)

        if profile["image"] and os.path.exists(profile["image"]):
            pix = QPixmap(profile["image"])
            size = 100
            circle_pix = QPixmap(size, size)
            circle_pix.fill(Qt.transparent)
            painter = QPainter(circle_pix)
            painter.setRenderHint(QPainter.Antialiasing, True)
            path = QPainterPath()
            path.addEllipse(0, 0, size, size)
            painter.setClipPath(path)
            scaled_pix = pix.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            x_offset = (size - scaled_pix.width()) // 2
            y_offset = (size - scaled_pix.height()) // 2
            painter.drawPixmap(x_offset, y_offset, scaled_pix)
            painter.end()
            icon_label = QLabel()
            icon_label.setFixedSize(size, size) 
            icon_label.setAlignment(Qt.AlignCenter) 
            icon_label.setPixmap(circle_pix)
            card_layout.addWidget(icon_label, alignment=Qt.AlignCenter)

        name_label = QLabel(profile["name"])
        name_label.setAlignment(Qt.AlignCenter)
        f = name_label.font()
        f.setPointSize(12)
        f.setBold(True)
        name_label.setFont(f)
        card_layout.addWidget(name_label)

        rides_label = QLabel(f"Przejazdy: {num_rides}")
        rides_label.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(rides_label)

        if avg_penalty != 0:
            grade_label = QLabel(f"Ocena: {grade}, Średnia kara: {avg_penalty:.2f}")
        else:
            grade_label = QLabel("Ocena: S, Brak kar")
        grade_label.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(grade_label)

        return card

    def calculate_profile_penalty(self, profile):
        total_segments = 0
        total_penalty = 0
        for ride in profile["rides"]:
            if os.path.exists(ride):
                segments = parse_segments_from_extended_data(ride)
                for s in segments:
                    if "speed_difference" in s:
                        diff = s["speed_difference"]
                        # 0 lub mniej = 0 pkt
                        # 0 < diff <=10 = 2 pkt
                        # diff >10 = 6 pkt
                        if diff > 10:
                            penalty = 6
                        elif diff > 0:
                            penalty = 2
                        else:
                            penalty = 0
                        total_penalty += penalty
                        total_segments += 1
        if total_segments == 0:
            return 0.0
        return total_penalty / total_segments

    def penalty_to_grade(self, avg_penalty):
        # Skala:
        # S: =0
        # A: 0 < avg ≤0.2
        # B: 0.2 < avg ≤0.4
        # C: 0.4 < avg ≤0.6
        # D: 0.6 < avg ≤0.8
        # E: 0.8 < avg ≤1.0
        # F: >1.0
        if avg_penalty == 0:
            return "S"
        elif avg_penalty <= 0.2:
            return "A"
        elif avg_penalty <= 0.4:
            return "B"
        elif avg_penalty <= 0.6:
            return "C"
        elif avg_penalty <= 0.8:
            return "D"
        elif avg_penalty <= 1.0:
            return "E"
        else:
            return "F"

    def initialize_map(self):
        default_location = [51.9194, 19.1451]
        self.map = folium.Map(location=default_location, zoom_start=6)
        self.add_responsive_css()
        map_file = 'default_map.html'
        self.map.save(map_file)
        self.web_view.load(QUrl.fromLocalFile(os.path.abspath(map_file)))

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
         &nbsp;<i style="background:green;color:green;">____</i>&nbsp; ≤ Limit<br>
         &nbsp;<i style="background:orange;color:orange;">____</i>&nbsp; ≤ Limit + 10 km/h<br>
         &nbsp;<i style="background:red;color:red;">____</i>&nbsp; > Limit + 10 km/h
         </div>
         """
        self.map.get_root().html.add_child(folium.Element(legend_html))

    def build_profile_tree(self):
        self.profile_tree.clear()
        for p in self.profiles_data["profiles"]:
            profile_item = QTreeWidgetItem([p["name"]])
            if p["image"] and os.path.exists(p["image"]):
                pix = QPixmap(p["image"])
                target_size = 64
                circle_pix = QPixmap(target_size, target_size)
                circle_pix.fill(Qt.transparent)

                painter = QPainter(circle_pix)
                painter.setRenderHint(QPainter.Antialiasing, True)

                path = QPainterPath()
                path.addEllipse(0, 0, target_size, target_size)
                painter.setClipPath(path)
                
                scaled_pix = pix.scaled(
                    target_size, target_size, 
                    Qt.KeepAspectRatio, 
                    Qt.SmoothTransformation
                )
                x_offset = (target_size - scaled_pix.width()) // 2
                y_offset = (target_size - scaled_pix.height()) // 2
                painter.drawPixmap(x_offset, y_offset, scaled_pix)
                painter.end()

                icon = QIcon(circle_pix)
                profile_item.setIcon(0, icon)

            self.profile_tree.addTopLevelItem(profile_item)

            for ride in p["rides"]:
                ride_item = QTreeWidgetItem([os.path.basename(ride)])
                ride_item.setData(0, Qt.UserRole, ride)
                profile_item.addChild(ride_item)

        self.profile_tree.expandAll()
        self.update_profiles_view()

    def tree_item_clicked(self, item, column):
        # jeśli to child - przejazd
        if item.parent() is not None:
            kml_file = item.data(0, Qt.UserRole)
            if kml_file and os.path.exists(kml_file):
                processed_segments = parse_segments_from_extended_data(kml_file)
                if not processed_segments:
                    self.status_label.setText("Brak danych ExtendedData dla tego przejazdu.")
                    return
                self.generate_map(processed_segments, kml_file)

    def remove_selected_items(self):
        items = self.profile_tree.selectedItems()
        if not items:
            return

        msg = QMessageBox.question(self, "Potwierdzenie",
                                   "Czy na pewno chcesz usunąć zaznaczone elementy?",
                                   QMessageBox.Yes | QMessageBox.No)
        if msg != QMessageBox.Yes:
            return

        profiles_to_remove = []
        rides_to_remove = []
        for it in items:
            parent = it.parent()
            if parent is None:
                profile_name = it.text(0)
                profiles_to_remove.append(profile_name)
            else:
                kml_file = it.data(0, Qt.UserRole)
                if kml_file:
                    profile_name = parent.text(0)
                    rides_to_remove.append((profile_name, kml_file))

        if profiles_to_remove:
            self.profiles_data["profiles"] = [p for p in self.profiles_data["profiles"] if p["name"] not in profiles_to_remove]

        for (pr_name, kml) in rides_to_remove:
            for p in self.profiles_data["profiles"]:
                if p["name"] == pr_name:
                    if kml in p["rides"]:
                        p["rides"].remove(kml)

        save_profiles(self.profiles_file, self.profiles_data)
        self.build_profile_tree()

    async def batch_speed_limits_query(self, points, batch_size=10):
        unique_points = list(set((round(p.y, 6), round(p.x, 6)) for p in points))
        results = {}
        overpass_url = "http://overpass-api.de/api/interpreter"
        
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
                                            continue

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
                                                if speed_limit_candidate is not None:
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
                speed_value = ''.join(filter(str.isdigit, speed_str))
                if speed_value:
                    return int(speed_value)
            except:
                pass
        return None

    def get_default_speed_limit_by_highway_type(self, highway_type):
        default_limits = {
            'motorway': 140,
            'trunk': 100,
            'primary': 50,
            'secondary': 50,
            'tertiary': 50,
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
        return results

    async def load_kml(self):
        options = QFileDialog.Options()
        kml_file, _ = QFileDialog.getOpenFileName(self, "Wybierz plik KML", "", 
                                                  "Pliki KML (*.kml);;Wszystkie pliki (*)", options=options)
        if not kml_file:
            return

        dlg = ProfileDialog(self.profiles_data, self)
        if dlg.exec_() == QDialog.Rejected:
            return

        profile_name = dlg.selected_profile
        if profile_name is None:
            profile_name = dlg.new_profile_name
            profile_photos_dir = 'profile_photos'
            if not os.path.exists(profile_photos_dir):
                os.makedirs(profile_photos_dir)
            new_image_path = None
            if dlg.new_profile_image:
                ext = os.path.splitext(dlg.new_profile_image)[1]
                unique_name = str(uuid.uuid4()) + ext
                new_image_path = os.path.join(profile_photos_dir, unique_name)
                try:
                    with open(dlg.new_profile_image, 'rb') as fin, open(new_image_path, 'wb') as fout:
                        fout.write(fin.read())
                except:
                    new_image_path = None

            new_profile = {
                "name": profile_name,
                "image": new_image_path if new_image_path else "",
                "rides": []
            }
            self.profiles_data["profiles"].append(new_profile)
            save_profiles(self.profiles_file, self.profiles_data)

        for p in self.profiles_data["profiles"]:
            if p["name"] == profile_name:
                if kml_file not in p["rides"]:
                    p["rides"].append(kml_file)
                    save_profiles(self.profiles_file, self.profiles_data)
                break

        self.load_button.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.status_label.setText("Wczytywanie pliku KML...")

        processed_segments = parse_segments_from_extended_data(kml_file)
        if processed_segments:
            self.status_label.setText("Wykryto istniejące dane prędkości. Generowanie mapy...")
            self.generate_map(processed_segments, kml_file)
            self.progress_bar.setVisible(False)
        else:
            gdf = gpd.read_file(kml_file, driver='KML')
            if gdf.empty:
                self.status_label.setText("Brak danych w pliku KML.")
                self.load_button.setEnabled(True)
                self.progress_bar.setVisible(False)
                return
            self.status_label.setText("Przetwarzanie danych...")

            if gdf.geometry.iloc[0].geom_type == 'LineString':
                segments = self.prepare_segments(gdf.geometry.iloc[0])
                
                self.progress_bar.setMaximum(len(segments))
                processed_segments = []
                
                batch_size = 10
                for i in range(0, len(segments), batch_size):
                    batch = segments[i:i + batch_size]
                    results = await self.process_segments_batch(batch)
                    processed_segments.extend(results)
                    self.progress_bar.setValue(len(processed_segments))
                    QApplication.processEvents()

                add_extended_data_to_kml(kml_file, processed_segments)
                self.generate_map(processed_segments, kml_file)
            else:
                self.status_label.setText("Geometria w pliku KML nie jest typu LineString.")

        self.load_button.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.build_profile_tree()

    def prepare_segments(self, line):
        coords = list(line.coords)
        time_per_segment = 1 / 360
        geod = Geod(ellps='WGS84')
        
        segments = []
        for i in range(len(coords) - 1):
            start = coords[i]
            end = coords[i + 1]
            _, _, distance = geod.inv(start[0], start[1], end[0], end[1])
            speed = (distance / 1000) / time_per_segment  
            segments.append({
                'geometry': LineString([start, end]),
                'speed': speed
            })
        return segments

    def generate_map(self, processed_segments, kml_filename):
        self.status_label.setText("Generowanie mapy...")

        seg_with_geom = [s for s in processed_segments if s.get('geometry') is not None]
        if not seg_with_geom:
            self.status_label.setText("Brak geometrii do wyświetlenia.")
            return

        segments_gdf = gpd.GeoDataFrame(seg_with_geom, crs="EPSG:4326")
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
            highlight_function=lambda x: {'weight': 8, 'color': 'blue'},
            popup=folium.GeoJsonPopup(fields=['speed', 'speed_limit'], aliases=['Prędkość:', 'Limit prędkości:'])
        ).add_to(self.map)

        bounds = segments_gdf.total_bounds
        self.map.fit_bounds([[bounds[1], bounds[0]], [bounds[3], bounds[2]]])

        self.add_legend()
        self.add_responsive_css()

        map_filename = os.path.splitext(os.path.basename(kml_filename))[0] + '_map.html'
        if not os.path.exists(self.maps_dir):
            os.makedirs(self.maps_dir)
        map_file = os.path.join(self.maps_dir, map_filename)
        self.map.save(map_file)
        
        self.map_files[kml_filename] = map_file
        self.web_view.load(QUrl.fromLocalFile(os.path.abspath(map_file)))
        
        self.status_label.setText("Mapa została wygenerowana.")


if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = MapApp()
    window.show()
    sys.exit(app.exec_())
