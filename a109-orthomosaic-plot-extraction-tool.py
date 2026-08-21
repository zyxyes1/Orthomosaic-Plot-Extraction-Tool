# -*- coding: utf-8 -*-
"""
Orthomosaic Plot Extraction Tool (OPET)
Extract pseudo-RGB patches for each plot from an orthomosaic TIFF,
using pre-defined corner coordinates (GPS) provided in a TXT file.
Supports batch processing of multiple TIFFs in a folder.
"""

import sys
import os
import json
import math
import numpy as np
import csv
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                            QHBoxLayout, QPushButton, QFileDialog, QLabel,
                            QScrollArea, QMessageBox, QGroupBox, QLineEdit,
                            QGridLayout)
from PyQt5.QtGui import QPixmap, QImage, QPainter, QPen, QColor, QFont, QMouseEvent
from PyQt5.QtCore import Qt, QPoint, QDateTime

try:
    from osgeo import gdal, osr
    gdal_available = True
except ImportError:
    gdal_available = False
    print("GDAL not available")

try:
    from skimage.draw import polygon
    skimage_available = True
except ImportError:
    skimage_available = False
    print("Warning: scikit-image not installed, polygon fill will fallback.")

try:
    from scipy.spatial import ConvexHull
    convex_hull_available = True
except ImportError:
    convex_hull_available = False
    print("Warning: scipy.spatial not installed, using fallback convex hull (may be unstable).")

class GeoCoordinateConverter:
    """Geographic coordinate converter between pixel and lat/lon."""
    
    def __init__(self, geo_transform=None, projection=None):
        self.geo_transform = geo_transform
        self.projection = projection
        self.has_geo_info = geo_transform is not None and projection is not None
        
        if self.has_geo_info:
            try:
                self.source_srs = osr.SpatialReference()
                self.source_srs.ImportFromWkt(projection)
                
                self.target_srs = osr.SpatialReference()
                self.target_srs.ImportFromEPSG(4326)  # WGS84
                
                self.transform_to_latlon = osr.CoordinateTransformation(self.source_srs, self.target_srs)
                self.transform_from_latlon = osr.CoordinateTransformation(self.target_srs, self.source_srs)
                
                print("Coordinate converter initialized successfully")
                
            except Exception as e:
                print(f"Failed to initialize coordinate converter: {str(e)}")
                self.has_geo_info = False
    
    def pixel_to_latlon(self, x, y):
        """Convert pixel coordinates to latitude/longitude."""
        if not self.has_geo_info or not self.geo_transform:
            return None
            
        try:
            geo_x = self.geo_transform[0] + x * self.geo_transform[1] + y * self.geo_transform[2]
            geo_y = self.geo_transform[3] + x * self.geo_transform[4] + y * self.geo_transform[5]
            
            latlon = self.transform_to_latlon.TransformPoint(geo_x, geo_y)
            return (latlon[1], latlon[0])  # lat, lon
        except Exception as e:
            print(f"Pixel to lat/lon failed: {str(e)}")
            return None

    def latlon_to_pixel(self, lon, lat):
        """Convert longitude/latitude to pixel coordinates (float)."""
        if not self.has_geo_info or not self.geo_transform:
            return None
            
        try:
            point = self.transform_from_latlon.TransformPoint(lon, lat)
            geo_x, geo_y = point[0], point[1]
            
            det = self.geo_transform[1] * self.geo_transform[5] - self.geo_transform[2] * self.geo_transform[4]
            
            if det == 0:
                return None
                
            x = (self.geo_transform[5] * (geo_x - self.geo_transform[0]) - 
                 self.geo_transform[2] * (geo_y - self.geo_transform[3])) / det
            y = (-self.geo_transform[4] * (geo_x - self.geo_transform[0]) + 
                 self.geo_transform[1] * (geo_y - self.geo_transform[3])) / det
                 
            return (x, y)  # float pixel coords
        except Exception as e:
            print(f"Lat/lon to pixel failed: {str(e)}")
            return None

class MaskImageLabel(QLabel):
    """Image display label with mask overlay (view-only)."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background-color: #f0f0f0;")
        self.setMinimumSize(1, 1)
        
        self.original_pixmap = None
        self.current_pixmap = None
        
        self.scale_factor = 1.0
        self.offset = QPoint(0, 0)
        self.dragging = False
        self.last_pos = QPoint(0, 0)
        
        self.mask_rects = []          # store polygons
        self.geo_converter = None
        
        self.last_update_time = QDateTime.currentDateTime().toMSecsSinceEpoch()
        self.update_interval = 16
    
    def set_original_pixmap(self, pixmap):
        self.original_pixmap = pixmap
        self.current_pixmap = pixmap
        self.setMinimumSize(pixmap.size())
        self.reset_view()
    
    def set_geo_converter(self, geo_converter):
        self.geo_converter = geo_converter
    
    def reset_view(self):
        self.scale_factor = 1.0
        self.offset = QPoint(0, 0)
        if self.original_pixmap:
            self.update_display()
    
    def zoom_in(self):
        if self.original_pixmap:
            self.scale_factor *= 1.2
            self.update_display()
    
    def zoom_out(self):
        if self.original_pixmap:
            self.scale_factor /= 1.2
            self.update_display()
    
    def transform_point(self, point):
        if not self.current_pixmap:
            return point
        
        scaled_width = self.current_pixmap.width() * self.scale_factor
        scaled_height = self.current_pixmap.height() * self.scale_factor
        
        display_x = (self.width() - scaled_width) / 2 + self.offset.x()
        display_y = (self.height() - scaled_height) / 2 + self.offset.y()
        
        img_x = (point.x() - display_x) / self.scale_factor
        img_y = (point.y() - display_y) / self.scale_factor
        
        return QPoint(int(img_x), int(img_y))
    
    def transform_screen_point(self, point):
        if not self.current_pixmap:
            return point
        
        scaled_width = self.current_pixmap.width() * self.scale_factor
        scaled_height = self.current_pixmap.height() * self.scale_factor
        
        display_x = (self.width() - scaled_width) / 2 + self.offset.x()
        display_y = (self.height() - scaled_height) / 2 + self.offset.y()
        
        screen_x = point.x() * self.scale_factor + display_x
        screen_y = point.y() * self.scale_factor + display_y
        
        return QPoint(int(screen_x), int(screen_y))
    
    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton and self.current_pixmap:
            self.dragging = True
            self.last_pos = event.pos()
    
    def mouseMoveEvent(self, event: QMouseEvent):
        current_time = QDateTime.currentDateTime().toMSecsSinceEpoch()
        if current_time - self.last_update_time < self.update_interval:
            return
            
        if self.dragging and self.current_pixmap:
            delta = event.pos() - self.last_pos
            self.offset += delta
            self.last_pos = event.pos()
            self.last_update_time = current_time
            self.update_display()
    
    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.dragging = False
    
    def update_display(self):
        if not self.current_pixmap:
            return
        
        canvas = QPixmap(self.size())
        canvas.fill(Qt.white)
        
        painter = QPainter(canvas)
        
        scaled_width = self.current_pixmap.width() * self.scale_factor
        scaled_height = self.current_pixmap.height() * self.scale_factor
        
        display_x = (self.width() - scaled_width) / 2 + self.offset.x()
        display_y = (self.height() - scaled_height) / 2 + self.offset.y()
        
        scaled_pixmap = self.current_pixmap.scaled(
            int(scaled_width),
            int(scaled_height),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation
        )
        painter.drawPixmap(int(display_x), int(display_y), scaled_pixmap)
        
        # Draw mask polygons (red borders)
        if self.mask_rects:
            pen = QPen(QColor(255, 0, 0))
            pen.setWidth(2)
            painter.setPen(pen)
            
            for rect in self.mask_rects:
                corners = rect.get('corners', [])
                if len(corners) < 3:
                    continue
                for i in range(len(corners)):
                    start_point = self.transform_screen_point(corners[i])
                    end_point = self.transform_screen_point(corners[(i + 1) % len(corners)])
                    painter.drawLine(start_point, end_point)
                for corner in corners:
                    screen_point = self.transform_screen_point(corner)
                    painter.drawEllipse(screen_point, 3, 3)
                center_x = sum(c.x() for c in corners) / len(corners)
                center_y = sum(c.y() for c in corners) / len(corners)
                center_point = QPoint(int(center_x), int(center_y))
                center_screen = self.transform_screen_point(center_point)
                painter.setPen(QPen(QColor(255, 0, 0)))
                painter.setFont(QFont("Arial", 10, QFont.Bold))
                painter.drawText(center_screen, rect.get('name', f"Plot_{rect['number']}"))
        
        painter.end()
        super().setPixmap(canvas)
    
    def resizeEvent(self, event):
        if self.current_pixmap:
            self.update_display()
        super().resizeEvent(event)

class MaskTestWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Orthomosaic Plot Extraction Tool")
        self.resize(1200, 800)
        
        self.tiff_path = ""
        self.geo_converter = None
        self.base_geo_transform = None
        self.base_projection = None
        self.mask_rects = []          # store polygons
        
        self.result_save_folder = ""
        self.tif_cut_folder = ""
        
        self.init_ui()
    
    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        main_layout = QHBoxLayout(central_widget)
        
        left_panel = QWidget()
        left_panel.setMaximumWidth(500)
        left_layout = QGridLayout(left_panel)
        left_layout.setHorizontalSpacing(10)
        left_layout.setVerticalSpacing(10)
        
        # ---------- File operations ----------
        file_group = QGroupBox("File Operations")
        file_layout = QVBoxLayout(file_group)
        
        tiff_layout = QVBoxLayout()
        self.load_tiff_btn = QPushButton("Open TIFF File")
        self.load_tiff_btn.clicked.connect(self.load_tiff_file)
        tiff_layout.addWidget(self.load_tiff_btn)
        
        self.tiff_info_label = QLabel("No TIFF loaded")
        self.tiff_info_label.setWordWrap(True)
        self.tiff_info_label.setStyleSheet("background-color: #f8f8f8; padding: 5px; border: 1px solid #ddd;")
        tiff_layout.addWidget(self.tiff_info_label)
        file_layout.addLayout(tiff_layout)
        
        txt_layout = QVBoxLayout()
        self.load_txt_btn = QPushButton("Load TXT Mask")
        self.load_txt_btn.clicked.connect(self.load_txt_file)
        self.load_txt_btn.setEnabled(False)
        txt_layout.addWidget(self.load_txt_btn)
        
        self.txt_info_label = QLabel("No TXT loaded")
        self.txt_info_label.setWordWrap(True)
        self.txt_info_label.setStyleSheet("background-color: #f8f8f8; padding: 5px; border: 1px solid #ddd;")
        txt_layout.addWidget(self.txt_info_label)
        file_layout.addLayout(txt_layout)
        
        close_layout = QVBoxLayout()
        self.close_image_btn = QPushButton("Close Image")
        self.close_image_btn.clicked.connect(self.close_image)
        self.close_image_btn.setEnabled(False)
        close_layout.addWidget(self.close_image_btn)
        file_layout.addLayout(close_layout)
        
        left_layout.addWidget(file_group, 0, 0)
        
        # ---------- Folder operations ----------
        folder_group = QGroupBox("Folder Operations")
        folder_layout = QVBoxLayout(folder_group)
        
        result_layout = QVBoxLayout()
        self.select_result_folder_btn = QPushButton("Select Result Folder")
        self.select_result_folder_btn.clicked.connect(self.select_result_folder)
        result_layout.addWidget(self.select_result_folder_btn)
        
        self.result_folder_label = QLabel("No result folder selected")
        self.result_folder_label.setWordWrap(True)
        self.result_folder_label.setStyleSheet("background-color: #f8f8f8; padding: 5px; border: 1px solid #ddd;")
        result_layout.addWidget(self.result_folder_label)
        folder_layout.addLayout(result_layout)
        
        cut_folder_layout = QVBoxLayout()
        self.select_cut_folder_btn = QPushButton("Select TIFF Folder to Cut")
        self.select_cut_folder_btn.clicked.connect(self.select_cut_folder)
        cut_folder_layout.addWidget(self.select_cut_folder_btn)
        
        self.cut_folder_label = QLabel("No TIFF folder selected")
        self.cut_folder_label.setWordWrap(True)
        self.cut_folder_label.setStyleSheet("background-color: #f8f8f8; padding: 5px; border: 1px solid #ddd;")
        cut_folder_layout.addWidget(self.cut_folder_label)
        folder_layout.addLayout(cut_folder_layout)
        
        self.cut_tif_btn = QPushButton("Cut TIFFs by Lat/Lon (Batch)")
        self.cut_tif_btn.clicked.connect(self.cut_tif_by_latlon)
        self.cut_tif_btn.setEnabled(False)
        folder_layout.addWidget(self.cut_tif_btn)
        
        left_layout.addWidget(folder_group, 1, 0)
        
        # ---------- View controls ----------
        zoom_group = QGroupBox("View Controls")
        zoom_layout = QHBoxLayout(zoom_group)
        
        self.zoom_in_btn = QPushButton("Zoom In")
        self.zoom_in_btn.clicked.connect(self.zoom_in)
        self.zoom_in_btn.setEnabled(False)
        zoom_layout.addWidget(self.zoom_in_btn)
        
        self.zoom_out_btn = QPushButton("Zoom Out")
        self.zoom_out_btn.clicked.connect(self.zoom_out)
        self.zoom_out_btn.setEnabled(False)
        zoom_layout.addWidget(self.zoom_out_btn)
        
        self.reset_view_btn = QPushButton("Reset View")
        self.reset_view_btn.clicked.connect(self.reset_view)
        self.reset_view_btn.setEnabled(False)
        zoom_layout.addWidget(self.reset_view_btn)
        
        left_layout.addWidget(zoom_group, 2, 0)
        
        main_layout.addWidget(left_panel)
        
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        self.scroll_area = QScrollArea()
        self.image_label = MaskImageLabel()
        self.scroll_area.setWidget(self.image_label)
        right_layout.addWidget(self.scroll_area)
        main_layout.addWidget(right_panel)
    
    # ---------- Helper methods ----------
    def compute_convex_hull(self, points):
        pts = list(points)
        if len(pts) < 3:
            return pts
        pts.sort(key=lambda p: (p[1], p[0]))
        p0 = pts[0]
        def polar_angle(p):
            return math.atan2(p[1]-p0[1], p[0]-p0[0])
        def distance(p):
            return (p[0]-p0[0])**2 + (p[1]-p0[1])**2
        sorted_pts = sorted(pts[1:], key=lambda p: (polar_angle(p), distance(p)))
        unique_pts = []
        for p in sorted_pts:
            if unique_pts and polar_angle(p) == polar_angle(unique_pts[-1]):
                if distance(p) > distance(unique_pts[-1]):
                    unique_pts[-1] = p
            else:
                unique_pts.append(p)
        stack = [p0]
        for p in unique_pts:
            while len(stack) >= 2:
                p1 = stack[-2]
                p2 = stack[-1]
                cross = (p2[0]-p1[0])*(p[1]-p2[1]) - (p2[1]-p1[1])*(p[0]-p2[0])
                if cross > 0:
                    break
                else:
                    stack.pop()
            stack.append(p)
        return stack
    
    # ---------- Load TXT ----------
    def load_txt_file(self):
        if not self.tiff_path:
            QMessageBox.warning(self, "Warning", "Please load a TIFF file first")
            return
        file_path, _ = QFileDialog.getOpenFileName(self, "Select TXT Mask File", "", "TXT files (*.txt)")
        if file_path:
            self.load_txt_info(file_path)
    
    def load_txt_info(self, file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            rects = []
            for line_num, line in enumerate(lines, 1):
                parts = line.strip().split()
                if len(parts) < 3:
                    continue
                name = parts[0]
                coords = parts[1:]
                if len(coords) % 2 != 0:
                    continue
                corners_geo = []
                for i in range(0, len(coords), 2):
                    try:
                        lon = float(coords[i])
                        lat = float(coords[i+1])
                        corners_geo.append([lon, lat])
                    except ValueError:
                        continue
                if len(corners_geo) < 3:
                    continue
                if convex_hull_available:
                    points = np.array(corners_geo)
                    hull = ConvexHull(points)
                    ordered_geo = points[hull.vertices].tolist()
                else:
                    ordered_geo = self.compute_convex_hull(corners_geo)
                # Convert to pixel coordinates for display
                ordered_corners = []
                for lon, lat in ordered_geo:
                    if self.geo_converter:
                        pixel = self.geo_converter.latlon_to_pixel(lon, lat)
                        if pixel:
                            x, y = pixel
                            ordered_corners.append(QPoint(int(round(x)), int(round(y))))
                        else:
                            ordered_corners.append(QPoint(0, 0))
                    else:
                        ordered_corners.append(QPoint(0, 0))
                rect_data = {
                    'number': line_num,
                    'name': name,
                    'corners_geo': ordered_geo,
                    'corners': ordered_corners
                }
                rects.append(rect_data)
            info_text = f"TXT file: {os.path.basename(file_path)}\nNumber of plots: {len(rects)}\n"
            self.txt_info_label.setText(info_text)
            self.mask_rects = rects
            self.image_label.mask_rects = self.mask_rects
            self.image_label.update_display()
            self.check_cut_button_state()
            QMessageBox.information(self, "Success", f"Loaded {len(rects)} plot masks")
        except Exception as e:
            self.txt_info_label.setText(f"Error: {str(e)}")
            QMessageBox.critical(self, "Error", f"Failed to load TXT file: {str(e)}")
    
    # ---------- Folder selection ----------
    def select_result_folder(self):
        folder_path = QFileDialog.getExistingDirectory(self, "Select Result Folder")
        if folder_path:
            self.result_save_folder = folder_path
            self.result_folder_label.setText(f"Result folder: {folder_path}")
            self.check_cut_button_state()
    
    def select_cut_folder(self):
        folder_path = QFileDialog.getExistingDirectory(self, "Select Folder Containing TIFFs")
        if folder_path:
            self.tif_cut_folder = folder_path
            self.cut_folder_label.setText(f"TIFF folder: {folder_path}")
            self.check_cut_button_state()
    
    def check_cut_button_state(self):
        if self.result_save_folder and self.tif_cut_folder and self.mask_rects:
            self.cut_tif_btn.setEnabled(True)
        else:
            self.cut_tif_btn.setEnabled(False)
    
    # ========== Core cutting function ==========
    def crop_polygon_from_tif_mem(self, dataset, polygon_geo, output_path):
        """
        Crop polygon area from in-memory TIFF using geographic coordinates and save as GeoTIFF.
        Returns (success, mean_value_inside_polygon).
        """
        try:
            geo_transform = dataset.GetGeoTransform()
            proj = dataset.GetProjection()
            width = dataset.RasterXSize
            height = dataset.RasterYSize
            
            # Convert geographic coordinates to float pixel coordinates
            pixel_points = []
            for lon, lat in polygon_geo:
                px = self.geo_converter.latlon_to_pixel(lon, lat)
                if px is None:
                    print(f"Coordinate conversion failed: ({lon}, {lat})")
                    return False, 0.0
                pixel_points.append(px)  # (x, y) float
            
            # Bounding rectangle
            xs = [p[0] for p in pixel_points]
            ys = [p[1] for p in pixel_points]
            min_x = max(0, int(math.floor(min(xs))) - 1)
            max_x = min(width - 1, int(math.ceil(max(xs))) + 1)
            min_y = max(0, int(math.floor(min(ys))) - 1)
            max_y = min(height - 1, int(math.ceil(max(ys))) + 1)
            
            crop_width = max_x - min_x + 1
            crop_height = max_y - min_y + 1
            if crop_width <= 0 or crop_height <= 0:
                print("Invalid crop region")
                return False, 0.0
            
            # Read all bands
            band_count = dataset.RasterCount
            sub_data = []
            for i in range(1, band_count+1):
                band = dataset.GetRasterBand(i)
                data = band.ReadAsArray(min_x, min_y, crop_width, crop_height)
                sub_data.append(data)
            sub_data = np.stack(sub_data, axis=0)  # (bands, h, w)
            
            # Build polygon mask (relative coordinates)
            rel_points = [(p[0] - min_x, p[1] - min_y) for p in pixel_points]
            
            if skimage_available:
                # polygon expects row (y) first, then column (x)
                rows = [int(round(y)) for x, y in rel_points]
                cols = [int(round(x)) for x, y in rel_points]
                # Clamp to crop bounds
                rows = [max(0, min(crop_height-1, r)) for r in rows]
                cols = [max(0, min(crop_width-1, c)) for c in cols]
                rr, cc = polygon(rows, cols, shape=(crop_height, crop_width))
                mask = np.zeros((crop_height, crop_width), dtype=bool)
                mask[rr, cc] = True
            else:
                mask = np.ones((crop_height, crop_width), dtype=bool)
                print("Warning: scikit-image not available, mask all kept")
            
            # Determine background value
            band0 = dataset.GetRasterBand(1)
            data_type = band0.DataType
            if data_type == gdal.GDT_Byte:
                background = 255
            else:
                background = 0
            
            # Apply mask: set outside to background
            masked_data = sub_data.copy()
            for b in range(masked_data.shape[0]):
                band_data = masked_data[b]
                band_data[~mask] = background
            
            # Compute mean inside mask
            masked_inner = sub_data[:, mask]
            valid_vals = masked_inner[~np.isnan(masked_inner)]
            mean_value = np.mean(valid_vals) if valid_vals.size > 0 else 0.0
            
            # Save as GeoTIFF
            driver = gdal.GetDriverByName('GTiff')
            out_ds = driver.Create(output_path, crop_width, crop_height, band_count, data_type)
            new_geo = list(geo_transform)
            new_geo[0] = geo_transform[0] + min_x * geo_transform[1] + min_y * geo_transform[2]
            new_geo[3] = geo_transform[3] + min_x * geo_transform[4] + min_y * geo_transform[5]
            out_ds.SetGeoTransform(tuple(new_geo))
            out_ds.SetProjection(proj)
            for b in range(band_count):
                out_band = out_ds.GetRasterBand(b+1)
                # Convert to original data type
                if data_type == gdal.GDT_Byte:
                    out_data = masked_data[b].astype(np.uint8)
                elif data_type == gdal.GDT_UInt16:
                    out_data = masked_data[b].astype(np.uint16)
                elif data_type == gdal.GDT_Int16:
                    out_data = masked_data[b].astype(np.int16)
                elif data_type == gdal.GDT_UInt32:
                    out_data = masked_data[b].astype(np.uint32)
                elif data_type == gdal.GDT_Int32:
                    out_data = masked_data[b].astype(np.int32)
                else:
                    out_data = masked_data[b].astype(np.float32)
                out_band.WriteArray(out_data)
                out_band.FlushCache()
            out_ds = None
            
            return True, float(mean_value)
            
        except Exception as e:
            print(f"Cropping failed: {str(e)}")
            import traceback
            traceback.print_exc()
            return False, 0.0
    
    # ---------- Main cutting function ----------
    def cut_tif_by_latlon(self):
        if not self.result_save_folder:
            QMessageBox.warning(self, "Warning", "Please select a result folder first")
            return
        if not self.tif_cut_folder:
            QMessageBox.warning(self, "Warning", "Please select a TIFF folder first")
            return
        if not self.mask_rects:
            QMessageBox.warning(self, "Warning", "Please load a TXT mask file first")
            return
        
        tif_files = [f for f in os.listdir(self.tif_cut_folder) if f.lower().endswith(('.tif', '.tiff'))]
        if not tif_files:
            QMessageBox.warning(self, "Warning", "No TIFF files found in the selected folder")
            return
        
        processed_count = 0
        total_plots = 0
        for tif_file in tif_files:
            tif_path = os.path.join(self.tif_cut_folder, tif_file)
            success, plots = self.process_single_tif_external_cut(tif_path)
            if success:
                processed_count += 1
                total_plots += plots
        
        QMessageBox.information(self, "Success", f"Processed {processed_count} files, generated {total_plots} plot images")
    
    def process_single_tif_external_cut(self, tif_path):
        try:
            dataset = gdal.Open(tif_path)
            if not dataset:
                print(f"Cannot open file: {tif_path}")
                return False, 0
            
            geo_transform = dataset.GetGeoTransform()
            projection = dataset.GetProjection()
            if not geo_transform or geo_transform == (0.0, 1.0, 0.0, 0.0, 0.0, 1.0):
                print(f"File {tif_path} lacks georeferencing, skipping")
                dataset = None
                return False, 0
            
            cur_converter = GeoCoordinateConverter(geo_transform, projection)
            if not cur_converter.has_geo_info:
                print(f"File {tif_path} cannot create coordinate converter, skipping")
                dataset = None
                return False, 0
            
            original_converter = self.geo_converter
            self.geo_converter = cur_converter
            
            base_name = os.path.splitext(os.path.basename(tif_path))[0]
            output_dir = os.path.join(self.result_save_folder, base_name)
            os.makedirs(output_dir, exist_ok=True)
            
            plot_count = 0
            for rect in self.mask_rects:
                corners_geo = rect.get('corners_geo', [])
                if len(corners_geo) < 3:
                    continue
                name = rect.get('name', f"Plot_{rect['number']}")
                output_path = os.path.join(output_dir, f"{base_name}_{name}.tif")
                success, mean_val = self.crop_polygon_from_tif_mem(dataset, corners_geo, output_path)
                if success:
                    plot_count += 1
                    print(f"  Saved: {output_path}, mean: {mean_val:.6f}")
                else:
                    print(f"  Failed to crop: {name}")
            
            self.geo_converter = original_converter
            dataset = None
            return True, plot_count
            
        except Exception as e:
            print(f"Error processing {tif_path}: {str(e)}")
            import traceback
            traceback.print_exc()
            return False, 0
    
    # ---------- Image display related ----------
    def close_image(self):
        self.image_label.original_pixmap = None
        self.image_label.current_pixmap = None
        self.image_label.clear()
        self.image_label.setStyleSheet("background-color: #f0f0f0;")
        self.image_label.setText("Load a TIFF file")
        self.image_label.mask_rects = []
        self.tiff_path = ""
        self.geo_converter = None
        self.base_geo_transform = None
        self.base_projection = None
        self.tiff_info_label.setText("No TIFF loaded")
        self.txt_info_label.setText("No TXT loaded")
        self.zoom_in_btn.setEnabled(False)
        self.zoom_out_btn.setEnabled(False)
        self.reset_view_btn.setEnabled(False)
        self.load_txt_btn.setEnabled(False)
        self.close_image_btn.setEnabled(False)
        self.check_cut_button_state()
        print("Image closed")
    
    def load_tiff_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Select TIFF File", "", "TIFF files (*.tif *.tiff)")
        if file_path:
            self.tiff_path = file_path
            self.load_tiff_info(file_path)
            self.close_image_btn.setEnabled(True)
    
    def apply_black_white_colormap(self, data):
        print(f"Applying black-white colormap, data shape: {data.shape}, dtype: {data.dtype}")
        if np.isnan(data).any() or np.isinf(data).any():
            data = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)
        min_val = np.min(data)
        max_val = np.max(data)
        if max_val > min_val:
            normalized = (data - min_val) / (max_val - min_val)
        else:
            normalized = np.zeros_like(data, dtype=np.float32)
        bw_array = (normalized * 255).astype(np.uint8)
        h, w = bw_array.shape
        rgb_array = np.zeros((h, w, 3), dtype=np.uint8)
        rgb_array[:, :, 0] = bw_array
        rgb_array[:, :, 1] = bw_array
        rgb_array[:, :, 2] = bw_array
        return rgb_array

    def apply_rgb_colormap(self, data):
        if len(data.shape) == 2:
            return self.apply_black_white_colormap(data)
        bands, h, w = data.shape
        if bands < 3:
            return self.apply_black_white_colormap(data[0])
        rgb_data = np.zeros((h, w, 3), dtype=np.uint8)
        for i in range(3):
            band = data[i]
            if np.isnan(band).any() or np.isinf(band).any():
                band = np.nan_to_num(band, nan=0.0, posinf=0.0, neginf=0.0)
            min_val = np.min(band)
            max_val = np.max(band)
            if max_val > min_val:
                norm = ((band - min_val) / (max_val - min_val) * 255).astype(np.uint8)
            else:
                norm = np.zeros_like(band, dtype=np.uint8)
            rgb_data[:, :, i] = norm
        return rgb_data

    def load_tiff_info(self, file_path):
        try:
            if not gdal_available:
                self.tiff_info_label.setText("Error: GDAL library not available")
                return
            dataset = gdal.Open(file_path)
            if not dataset:
                self.tiff_info_label.setText("Error: Cannot open TIFF file")
                return
            width = dataset.RasterXSize
            height = dataset.RasterYSize
            bands = dataset.RasterCount
            geo_transform = dataset.GetGeoTransform()
            projection = dataset.GetProjection()
            info_text = f"TIFF file: {os.path.basename(file_path)}\n"
            info_text += f"Size: {width} × {height}\n"
            info_text += f"Bands: {bands}\n"
            if geo_transform and geo_transform != (0.0, 1.0, 0.0, 0.0, 0.0, 1.0):
                self.geo_converter = GeoCoordinateConverter(geo_transform, projection)
                self.base_geo_transform = geo_transform
                self.base_projection = projection
                self.image_label.set_geo_converter(self.geo_converter)
                info_text += "Georeferencing: available\n"
            else:
                self.geo_converter = None
                self.base_geo_transform = None
                self.base_projection = None
                self.image_label.set_geo_converter(None)
                info_text += "Georeferencing: not available\n"
            self.tiff_info_label.setText(info_text)
            self.load_and_display_tiff(file_path)
            self.zoom_in_btn.setEnabled(True)
            self.zoom_out_btn.setEnabled(True)
            self.reset_view_btn.setEnabled(True)
            self.load_txt_btn.setEnabled(True)
            self.close_image_btn.setEnabled(True)
            self.check_cut_button_state()
            dataset = None
        except Exception as e:
            self.tiff_info_label.setText(f"Error: {str(e)}")
    
    def load_and_display_tiff(self, file_path):
        try:
            dataset = gdal.Open(file_path)
            if not dataset:
                return
            width = dataset.RasterXSize
            height = dataset.RasterYSize
            bands = dataset.RasterCount
            
            if bands >= 3:
                data = []
                for i in range(3):
                    band = dataset.GetRasterBand(i+1)
                    band_data = band.ReadAsArray(0, 0, width, height)
                    data.append(band_data)
                data = np.array(data)
                rgb_array = self.apply_rgb_colormap(data)
            else:
                band = dataset.GetRasterBand(1)
                data = band.ReadAsArray(0, 0, width, height)
                rgb_array = self.apply_black_white_colormap(data)
            
            height_img, width_img, _ = rgb_array.shape
            bytes_per_line = 3 * width_img
            q_image = QImage(rgb_array.data, width_img, height_img, 
                            bytes_per_line, QImage.Format_RGB888)
            pixmap = QPixmap.fromImage(q_image)
            self.image_label.set_original_pixmap(pixmap)
            dataset = None
            print(f"Image displayed: {width_img} × {height_img}, bands: {bands}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load image: {str(e)}")
            import traceback
            print(f"Detailed error: {traceback.format_exc()}")
    
    # ---------- View controls ----------
    def zoom_in(self):
        if self.image_label:
            self.image_label.zoom_in()
    
    def zoom_out(self):
        if self.image_label:
            self.image_label.zoom_out()
    
    def reset_view(self):
        if self.image_label:
            self.image_label.reset_view()

def main():
    app = QApplication(sys.argv)
    if not gdal_available:
        QMessageBox.critical(None, "Error", "GDAL library not available. Please install GDAL before running.")
        return 1
    window = MaskTestWindow()
    window.show()
    return app.exec_()

if __name__ == '__main__':
    main()
