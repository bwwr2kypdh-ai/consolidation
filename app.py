import streamlit as st
import folium
from streamlit_folium import st_folium
from folium.plugins import Draw
import pandas as pd
import numpy as np
from shapely.geometry import Polygon, Point
import math
import requests
import time
import json
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import matplotlib.pyplot as plt
import matplotlib.tri as tri

# ==========================================
# 1. CONFIGURATION & CSS
# ==========================================
st.set_page_config(layout="wide", page_title="Port Surcharge SIG PRO")
st.markdown("<style>@media print { .stSidebar {display: none !important;} }</style>", unsafe_allow_html=True)

if "lang" not in st.session_state: st.session_state["lang"] = "Français"
def tr(fr, en): return fr if st.session_state["lang"] == "Français" else en

if "authenticated" not in st.session_state: st.session_state["authenticated"] = False
if 'map_center' not in st.session_state: st.session_state['map_center'] = [43.2965, 5.3698]
if 'project_data' not in st.session_state: st.session_state['project_data'] = None

# ==========================================
# 2. AUTHENTICATION
# ==========================================
def check_password():
    def password_entered():
        if st.session_state["pwd_input"].strip().lower() == "admin":
            st.session_state["authenticated"] = True
            del st.session_state["pwd_input"] 
        else: st.session_state["authenticated"] = False

    if not st.session_state.get("authenticated", False):
        st.title("🔒 Accès Sécurisé SIG / Secure Access")
        st.text_input("Mot de passe (admin) :", type="password", on_change=password_entered, key="pwd_input")
        st.stop()
check_password()

# ==========================================
# 3. MOTEUR MATHÉMATIQUE & SIG
# ==========================================
def calculate_loads(dead_load, live_load, surcharge_ratio, gamma_fill):
    final_load = dead_load + live_load
    return final_load, final_load * surcharge_ratio, (final_load * surcharge_ratio) / gamma_fill

def calc_settlement_oedometer(layer, delta_sigma):
    fs = layer['sig_0'] + delta_sigma
    if fs <= layer['sig_c']: return layer['H'] * (layer['Cr'] / (1 + layer['e0'])) * math.log10(fs / layer['sig_0'])
    else: return (layer['H'] * (layer['Cr'] / (1 + layer['e0'])) * math.log10(layer['sig_c'] / layer['sig_0'])) + (layer['H'] * (layer['Cc'] / (1 + layer['e0'])) * math.log10(fs / layer['sig_c']))

def calc_settlement_cptu(layer, delta_sigma):
    return (delta_sigma / max(1000, layer['alpha'] * ((layer['qt'] * 1000) - layer['sig_v0']))) * layer['H']

def calc_settlement_spt(layer, delta_sigma):
    return (delta_sigma / max(1000, layer['f2'] * layer['N60'])) * layer['H']

def calc_secondary_compression(C_alpha, H, t_days, t_years):
    t1 = t_days / 365.25; t2 = t1 + t_years
    return (C_alpha * H * math.log10(t2 / t1)) if t1 > 0 else 0.0

def generate_pvd_grid(polygon_coords, spacing):
    poly = Polygon(polygon_coords)
    minx, miny, maxx, maxy = poly.bounds
    lat_to_m, lon_to_m = 111000, 111000 * math.cos(math.radians(miny))
    dx_deg = spacing / lon_to_m
    dy_deg = (spacing * math.sqrt(3) / 2) / lat_to_m
    pvds = []
    x_coords = np.arange(minx, maxx + dx_deg, dx_deg)
    y_coords = np.arange(miny, maxy + dy_deg, dy_deg)
    for i, y in enumerate(y_coords):
        offset = (dx_deg / 2) if i % 2 != 0 else 0
        for x in x_coords:
            pt = Point(x + offset, y)
            if poly.contains(pt): pvds.append({'Lon': pt.x, 'Lat': pt.y})
    return pd.DataFrame(pvds)

def hansbo_consolidation(ch_m2_yr, spacing, t_days):
    if t_days <= 0: return 0.0
    Tr = (ch_m2_yr / 365.25 * t_days) / ((1.05 * spacing)**2)
    try: return max(0.0, min(1.0 - math.exp((-8.0 * Tr) / (math.log((1.05 * spacing)/0.052) - 0.75)), 1.0))
    except: return 1.0

def calculate_asaoka(times, settlements, delta_t=10):
    if len(times) < 3: return None, None, None, None, None
    t_interp = np.arange(min(times), max(times) + delta_t, delta_t)
    s_interp = np.interp(t_interp, times, settlements)
    s_n_minus_1, s_n = s_interp[:-1], s_interp[1:]
    coef = np.polyfit(s_n_minus_1, s_n, 1)
    if coef[0] >= 1.0 or coef[0] <= 0: return None, None, None, None, None 
    return coef[1] / (1 - coef[0]), coef[1], coef[0], s_n_minus_1, s_n

# ==========================================
# 5. SIDEBAR : SAVE/LOAD, MNT, LOADS
# ==========================================
st.session_state["lang"] = st.sidebar.radio("🌐 Langue / Language", ["Français", "English"])
if st.sidebar.button(tr("Se déconnecter 🚪", "Logout")): st.session_state["authenticated"] = False; st.rerun()

st.sidebar.markdown("---")
with st.sidebar.expander(tr("💾 Sauvegarder / Charger (JSON)", "💾 Save / Load (JSON)")):
    uploaded_file = st.file_uploader(tr("Charger un projet (.json)", "Load a project (.json)"), type="json")
    if uploaded_file is not None:
        try:
            saved_state = json.load(uploaded_file)
            pd_data = saved_state.get('project_data', None)
            if pd_data:
                pd_data['mnt'] = pd.read_json(pd_data['mnt']) if pd_data.get('mnt') else None
                pd_data['pvds'] = pd.read_json(pd_data['pvds']) if pd_data.get('pvds') else None
                pd_data['results'] = pd.read_json(pd_data['results']) if pd_data.get('results') else None
                st.session_state['project_data'] = pd_data
            st.success(tr("Projet chargé !", "Project loaded!"))
        except Exception as e: st.error(f"Erreur/Error: {e}")
        
    if st.session_state['project_data'] is not None:
        export_data = st.session_state['project_data'].copy()
        export_data['mnt'] = export_data['mnt'].to_json() if isinstance(export_data.get('mnt'), pd.DataFrame) else None
        export_data['pvds'] = export_data['pvds'].to_json() if isinstance(export_data.get('pvds'), pd.DataFrame) else None
        export_data['results'] = export_data['results'].to_json() if isinstance(export_data.get('results'), pd.DataFrame) else None
        st.download_button(tr("📥 Exporter le Projet (JSON)", "📥 Export Project (JSON)"), data=json.dumps({'project_data': export_data}), file_name="projet_sig.json", mime="application/json")

st.sidebar.markdown("---")
st.sidebar.header(tr("📍 Localisation", "📍 Location"))
search_query = st.sidebar.text_input(tr("Adresse ou GPS", "Address or GPS"))
if st.sidebar.button(tr("Chercher", "Search")) and search_query:
    try:
        res = requests.get(f"https://nominatim.openstreetmap.org/search?q={search_query}&format=json&limit=1", headers={'User-Agent': 'TopoApp'}).json()
        if res: st.session_state['map_center'] = [float(res[0]['lat']), float(res[0]['lon'])]; st.rerun()
    except: pass

st.sidebar.header(tr("🗺️ Topographie (MNT)", "🗺️ Topography (DEM)"))
api_choice = st.sidebar.selectbox(tr("Source MNT", "DEM Source"), ["Open-Meteo", "Google Maps API", "Fichier CSV Local"])
api_key = st.sidebar.text_input("Clé API Google", type="password") if "Google" in api_choice else ""
uploaded_mnt = st.sidebar.file_uploader(tr("Importer MNT (.csv)", "Import DEM (.csv)"), type=['csv']) if "CSV" in api_choice else None

st.sidebar.markdown("---")
st.sidebar.header(tr("🏗️ Projet & Logistique", "🏗️ Project & Logistics"))
dead_load = st.sidebar.number_input("Charge Permanente [kPa]", value=20.0)
live_load = st.sidebar.number_input("Exploitation [kPa]", value=80.0)
gamma_fill = st.sidebar.number_input("Densité Remblai [kN/m³]", value=19.0)
surcharge_ratio = st.sidebar.slider("Surcharge Ratio", 1.0, 2.0, 1.2)
target_time = st.sidebar.number_input("Temps Cible (Jours)", value=180)
design_life = st.sidebar.number_input("Durée de vie ouvrage [Années]", value=30)

# ==========================================
# 6. MAIN UI & INTERACTIVE MAP (DRAWING ZONES)
# ==========================================
st.title(tr("⚓ Port Terminal - SIG Géotechnique PRO", "⚓ Port Terminal - PRO Geotechnical GIS"))
st.write(tr("Dessinez une ou plusieurs zones sur la carte. Chaque polygone créera un onglet de stratigraphie dans le menu de gauche.", "Draw one or more zones on the map. Each polygon will create a stratigraphy tab in the left menu."))

# Basculement de carte robuste via Streamlit
map_style = st.radio(tr("Vue de la carte :", "Map View :"), ["Satellite (Esri)", "Plan (CartoDB)", "OSM"], horizontal=True)
tiles_dict = {"Satellite (Esri)": 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', "Plan (CartoDB)": 'CartoDB Positron', "OSM": 'OpenStreetMap'}
attr_dict = {"Satellite (Esri)": 'Esri', "Plan (CartoDB)": 'CartoDB', "OSM": 'OSM'}

m = folium.Map(location=st.session_state['map_center'], zoom_start=15, tiles=tiles_dict[map_style], attr=attr_dict[map_style])
Draw(export=True, draw_options={'polyline':False, 'polygon':True, 'rectangle':True, 'circle':False, 'marker':False}).add_to(m)

col_map, col_action = st.columns([2, 1])
with col_map:
    output = st_folium(m, width=800, height=500, key="input_map")

# DÉTECTION DES POLYGONES DESSINÉS
drawn_polygons = []
if output and output.get("all_drawings"):
    drawn_polygons = [d["geometry"]["coordinates"][0] for d in output["all_drawings"] if d.get("geometry", {}).get("type") in ["Polygon", "Rectangle"]]

# ==========================================
# 7. DYNAMIC STRATIGRAPHY (ZONING)
# ==========================================
zones_data = []
if len(drawn_polygons) == 0:
    st.sidebar.warning(tr("Dessinez au moins une zone sur la carte.", "Draw at least one zone on the map."))
else:
    st.sidebar.header(tr("🗜️ Stratigraphie par Zone", "🗜️ Zoning Stratigraphy"))
    # C'est ici que les zones s'ajoutent dynamiquement !
    for i, poly_coords in enumerate(drawn_polygons):
        with st.sidebar.expander(f"🔴 Zone {i+1} Paramètres", expanded=(i==0)):
            H = st.number_input("Épaisseur Argile (m)", value=8.0, key=f"H_{i}")
            e0 = st.number_input("e0", value=1.20, key=f"e0_{i}")
            Cc = st.number_input("Cc", value=0.45, key=f"Cc_{i}")
            Cr = st.number_input("Cr", value=0.05, key=f"Cr_{i}")
            sig_0 = st.number_input("σ'0 [kPa]", value=40.0, key=f"s0_{i}")
            sig_c = st.number_input("σ'c [kPa]", value=45.0, key=f"sc_{i}")
            qt = st.number_input("qt (CPTu) [MPa]", value=0.60, key=f"qt_{i}")
            sig_v0 = st.number_input("σv0 (CPTu) [kPa]", value=50.0, key=f"sv0_{i}")
            alpha = st.number_input("α_M", value=4.0, key=f"a_{i}")
            N60 = st.number_input("N60 (SPT)", value=3.0, key=f"n60_{i}")
            f2 = st.number_input("f2 [kPa]", value=500.0, key=f"f2_{i}")
            C_alpha = st.number_input("C_alpha (Fluage)", value=0.015, format="%.3f", key=f"ca_{i}")
            Su = st.number_input("Su (Cohésion) [kPa]", value=15.0, key=f"su_{i}")
            ch = st.number_input("c_h [m²/yr]", value=2.0, key=f"ch_{i}")
            spacing = st.slider("Espacement PVD (m)", 0.8, 3.0, 1.2, key=f"sp_{i}")
            
            zones_data.append({
                'id': i+1, 'coords': poly_coords, 'H': H, 'e0': e0, 'Cc': Cc, 'Cr': Cr, 
                'sig_0': sig_0, 'sig_c': sig_c, 'qt': qt, 'sig_v0': sig_v0, 'alpha': alpha,
                'N60': N60, 'f2': f2, 'C_alpha': C_alpha, 'Su': Su, 'ch': ch, 'spacing': spacing
            })

# ==========================================
# 8. CALCULATION ENGINE
# ==========================================
with col_action:
    if st.button(tr("🚀 LANCER L'ANALYSE SIG", "🚀 RUN GIS ANALYSIS"), use_container_width=True, type="primary"):
        if not zones_data: st.error(tr("Aucune zone définie.", "No zones defined."))
        else:
            with st.spinner(tr("Génération Topographie & Tassements...", "Generating Topo & Settlement...")):
                final_load, target_load, base_fill = calculate_loads(dead_load, live_load, surcharge_ratio, gamma_fill)
                
                # --- MNT ACQUISITION ---
                df_mnt = None
                if "CSV" in api_choice and uploaded_mnt is not None:
                    try:
                        df_mnt = pd.read_csv(uploaded_mnt)
                        col_z = [c for c in df_mnt.columns if 'Z' in c.upper() or 'ELEV' in c.upper()][0]
                        df_mnt = df_mnt.rename(columns={col_z: 'Z'})
                    except Exception as e: st.error(f"Erreur CSV: {e}")
                else:
                    all_pts = [pt for zone in zones_data for pt in zone['coords']]
                    min_lon, max_lon = min(p[0] for p in all_pts), max(p[0] for p in all_pts)
                    min_lat, max_lat = min(p[1] for p in all_pts), max(p[1] for p in all_pts)
                    res = 15.0 / 111000.0
                    lons = np.arange(min_lon, max_lon + res, res)
                    lats = np.arange(min_lat, max_lat + res, res)
                    mnt_pts = [(lt, ln) for lt in lats for ln in lons]
                    
                    elevs = []
                    if "Google" in api_choice and api_key:
                        for i in range(0, len(mnt_pts), 50):
                            locs = "|".join([f"{lt},{ln}" for lt, ln in mnt_pts[i:i+50]])
                            try:
                                r = requests.get(f"https://maps.googleapis.com/maps/api/elevation/json?locations={locs}&key={api_key}").json()
                                if r.get('status') == 'OK': elevs.extend([res['elevation'] for res in r['results']])
                            except: break
                            time.sleep(0.1)
                    elif "Open-Meteo" in api_choice:
                        try:
                            lats_str, lons_str = ",".join([str(p[0]) for p in mnt_pts]), ",".join([str(p[1]) for p in mnt_pts])
                            r = requests.get(f"https://api.open-meteo.com/v1/elevation?latitude={lats_str}&longitude={lons_str}").json()
                            if 'elevation' in r: elevs = r['elevation']
                        except: pass
                    
                    if elevs and len(elevs) == len(mnt_pts): df_mnt = pd.DataFrame({'Lat': [p[0] for p in mnt_pts], 'Lon': [p[1] for p in mnt_pts], 'Z': elevs})
                    else:
                        # Fallback synthétique si API échoue
                        elevs = [2.0 + math.sin(lt*1000) for lt, ln in mnt_pts]
                        df_mnt = pd.DataFrame({'Lat': [p[0] for p in mnt_pts], 'Lon': [p[1] for p in mnt_pts], 'Z': elevs})

                # --- CALCULS PAR ZONE ---
                results_zones = []
                all_pvds = pd.DataFrame()
                
                for z in zones_data:
                    poly = Polygon(z['coords'])
                    area = poly.area * (111000**2) * math.cos(math.radians(poly.centroid.y))
                    
                    S_oedo = calc_settlement_oedometer(z, target_load)
                    S_cptu = calc_settlement_cptu(z, target_load)
                    S_spt = calc_settlement_spt(z, target_load)
                    S_max = max(S_oedo, S_cptu, S_spt)
                    
                    S_sec = calc_secondary_compression(z['C_alpha'], z['H'], target_time, design_life)
                    actual_fill = base_fill + S_max
                    vol = area * actual_fill
                    
                    q_ult = 5.14 * z['Su']
                    FS_mudwave = q_ult / (gamma_fill * actual_fill) if actual_fill > 0 else 999
                    
                    df_pvd = generate_pvd_grid(z['coords'], z['spacing'])
                    if not df_pvd.empty:
                        df_pvd['Zone'] = z['id']
                        df_pvd['H_drain'] = z['H']
                        all_pvds = pd.concat([all_pvds, df_pvd])
                    
                    results_zones.append({
                        'Zone': z['id'], 'Area': area, 'S_max': S_max, 'S_sec': S_sec,
                        'Fill_H': actual_fill, 'Vol': vol, 'FS_Mudwave': FS_mudwave, 'PVD_Count': len(df_pvd)
                    })
                
                st.session_state['project_data'] = {
                    'zones': zones_data, 'results': pd.DataFrame(results_zones), 
                    'pvds': all_pvds, 'mnt': df_mnt, 'target_load': target_load
                }
                st.success(tr("Analyse SIG Terminée !", "GIS Analysis Complete!"))

# ==========================================
# 9. RESULTS DASHBOARD
# ==========================================
if st.session_state['project_data'] is not None:
    d = st.session_state['project_data']
    st.markdown("---")
    
    t_topo, t_pvd, t_coupe, t_suivi, t_risk = st.tabs([
        tr("🗺️ Topographie & Tassements", "🗺️ Topography & Settlement"), 
        tr("📍 Implantation PVD", "📍 PVD Layout"), 
        tr("📐 Vues en Coupe", "📐 Cross Sections"),
        tr("📉 Suivi (Asaoka & Lifts)", "📉 Monitoring & Lifts"),
        tr("⚠️ Risques", "⚠️ Risks")
    ])
    
    # --- ONGLET 1 : CONTOURS (MNT & TASSEMENTS) ---
    with t_topo:
        if d['mnt'] is not None and not d['mnt'].empty:
            st.download_button(tr("📥 Télécharger le MNT Actuel (CSV)", "📥 Download Current DEM (CSV)"), data=d['mnt'].to_csv(index=False).encode('utf-8'), file_name='projet_mnt_sauvegarde.csv', mime='text/csv')
            
            c1, c2 = st.columns(2)
            try:
                triang = tri.Triangulation(d['mnt']['Lon'], d['mnt']['Lat'])
                with c1:
                    st.write("**Topographie Initiale (Z)**")
                    fig, ax = plt.subplots(figsize=(8, 6))
                    contour = ax.tricontourf(triang, d['mnt']['Z'], levels=15, cmap="terrain")
                    ax.tricontour(triang, d['mnt']['Z'], levels=15, colors='k', linewidths=0.5)
                    plt.colorbar(contour, ax=ax, label="Élévation (m MSL)")
                    for z in d['zones']:
                        x, y = Polygon(z['coords']).exterior.xy
                        ax.plot(x, y, color='red', linewidth=2, label=f"Zone {z['id']}")
                    st.pyplot(fig)
                    plt.close(fig)
                    
                with c2:
                    st.write("**Tassements Projetés (S_max)**")
                    df_settle = d['mnt'].copy()
                    df_settle['S'] = 0.0
                    for z, res in zip(d['zones'], d['results'].to_dict('records')):
                        poly = Polygon(z['coords'])
                        mask = df_settle.apply(lambda row: poly.contains(Point(row['Lon'], row['Lat'])), axis=1)
                        df_settle.loc[mask, 'S'] = res['S_max']
                    
                    fig2, ax2 = plt.subplots(figsize=(8, 6))
                    contour2 = ax2.tricontourf(triang, df_settle['S'], levels=15, cmap="YlOrRd")
                    plt.colorbar(contour2, ax=ax2, label="Tassement (m)")
                    for z in d['zones']:
                        x, y = Polygon(z['coords']).exterior.xy
                        ax2.plot(x, y, color='black', linewidth=1)
                    st.pyplot(fig2)
                    plt.close(fig2)
            except Exception as e: st.warning(f"Isolignes impossibles: {e}")
            
            # --- CARTE INTERACTIVE MNT SOUS-ÉCHANTILLONNÉE ---
            st.markdown("---")
            st.write(tr("**Carte Interactive du Maillage MNT**", "**Interactive DEM Grid Map**"))
            
            m_mnt = folium.Map(location=[d['mnt']['Lat'].mean(), d['mnt']['Lon'].mean()], zoom_start=16, tiles='CartoDB Positron')
            for z in d['zones']: folium.Polygon(locations=[(p[1], p[0]) for p in z['coords']], color='orange', weight=2, fill=False).add_to(m_mnt)
            
            for idx, row in d['mnt'].iterrows():
                folium.CircleMarker(location=[row['Lat'], row['Lon']], radius=0.5, color='blue', fill=True, fill_opacity=0.3, weight=0).add_to(m_mnt)
                
            lats_gardees = sorted(d['mnt']['Lat'].unique())[::4]
            lons_gardees = sorted(d['mnt']['Lon'].unique())[::4]
            df_text = d['mnt'][d['mnt']['Lat'].isin(lats_gardees) & d['mnt']['Lon'].isin(lons_gardees)]
            
            for idx, row in df_text.iterrows():
                folium.Marker(
                    location=[row['Lat'], row['Lon']],
                    icon=folium.DivIcon(html=f'<div style="font-size: 11px; font-weight: bold; color: #8B0000; text-shadow: 1px 1px 0px white, -1px -1px 0px white, 1px -1px 0px white, -1px 1px 0px white; white-space: nowrap;">{row["Z"]:.1f}</div>')
                ).add_to(m_mnt)
                
            st_folium(m_mnt, width=1200, height=500, key="mnt_grid_map")

    # --- ONGLET 2 : IMPLANTATION PVD (ÉCHANTILLONNAGE) ---
    with t_pvd:
        st.subheader("Plan d'Implantation Précise des Drains Verticaux")
        tot_pvd = len(d['pvds']) if not d['pvds'].empty else 0
        st.write(f"Nombre total de PVD à commander : **{tot_pvd:,.0f} unités**.")
        
        if not d['pvds'].empty:
            max_points = 3000
            if tot_pvd > max_points:
                step = tot_pvd // max_points
                df_plot_pvd = d['pvds'].iloc[::step]
                st.warning(f"⚠️ Affichage optimisé : 1 point sur {step} est dessiné pour fluidité.")
            else: df_plot_pvd = d['pvds']

            fig_pvd = go.Figure()
            for z in d['zones']:
                x, y = Polygon(z['coords']).exterior.xy
                fig_pvd.add_trace(go.Scattermapbox(lat=list(y), lon=list(x), mode='lines', line=dict(width=3, color='red'), name=f"Zone {z['id']}"))
            fig_pvd.add_trace(go.Scattermapbox(lat=df_plot_pvd['Lat'], lon=df_plot_pvd['Lon'], mode='markers', marker=go.scattermapbox.Marker(size=3, color='blue', opacity=0.6), name="PVDs"))
            fig_pvd.update_layout(mapbox_style="carto-positron", mapbox_zoom=15, mapbox_center={"lat": d['pvds']['Lat'].mean(), "lon": d['pvds']['Lon'].mean()}, height=600, margin={"r":0,"t":0,"l":0,"b":0})
            st.plotly_chart(fig_pvd, use_container_width=True)
            
            st.download_button(tr("📥 Télécharger TOUS les PVD (CSV)", "📥 Download ALL PVDs (CSV)"), data=d['pvds'].to_csv(index=False).encode('utf-8'), file_name='implantation_pvd.csv', mime='text/csv', type="primary")
            st.dataframe(d['results'], use_container_width=True)

    # --- ONGLET 3 : VUES EN COUPE ---
    with t_coupe:
        st.subheader("Profils Stratigraphiques et Remblai")
        zone_sel = st.selectbox("Sélectionnez la Zone :", [f"Zone {z['id']}" for z in d['zones']])
        z_idx = int(zone_sel.split(" ")[1]) - 1
        z_data, res_data = d['zones'][z_idx], d['results'].iloc[z_idx]
        
        fig_coupe = go.Figure()
        fig_coupe.add_trace(go.Scatter(x=[0, 100, 100, 0], y=[0, 0, -z_data['H'], -z_data['H']], fill='toself', fillcolor='saddlebrown', line=dict(color='black'), name="Argile Molle"))
        fig_coupe.add_trace(go.Scatter(x=[10, 90, 80, 20], y=[0, 0, res_data['Fill_H'], res_data['Fill_H']], fill='toself', fillcolor='orange', line=dict(color='black'), name="Surcharge"))
        fig_coupe.add_hline(y=-res_data['S_max'], line_dash="dash", line_color="red", annotation_text=f"Tassement Max (-{res_data['S_max']:.2f}m)")
        fig_coupe.update_layout(title=f"Coupe Transversale - {zone_sel}", xaxis_title="Distance (m)", yaxis_title="Élévation (m)", height=400)
        st.plotly_chart(fig_coupe, use_container_width=True)

    # --- ONGLET 4 : MONITORING & LIFTS ---
    with t_suivi:
        st.subheader("Monitoring & Exécution")
        zone_suivi = st.selectbox("Sélectionner la Zone monitorée :", [f"Zone {z['id']}" for z in d['zones']], key="suivi_zone")
        zs_idx = int(zone_suivi.split(" ")[1]) - 1
        z_suivi_data, res_suivi = d['zones'][zs_idx], d['results'].iloc[zs_idx]
        S_max_theorique, H_act_requis = res_suivi['S_max'], res_suivi['Fill_H']
        
        c_m1, c_m2 = st.columns([1, 2])
        with c_m1:
            st.write("**Phasage Remblai (Lifts)**")
            e_lifts = st.data_editor(pd.DataFrame({'Jour': [0, 15, 45, 75], 'Levée_m': [0.5, 1.5, 1.5, 1.0]}), num_rows="dynamic", use_container_width=True, key=f"l_{zs_idx}").sort_values(by='Jour')
            st.write("**Relevés Tassement (Asaoka)**")
            e_mon = st.data_editor(pd.DataFrame({'Jour': [0, 15, 30, 45, 60], 'Relevé (m)': [0.0, 0.10, 0.25, 0.40, 0.55]}), num_rows="dynamic", use_container_width=True, key=f"m_{zs_idx}").sort_values(by='Jour')
        
        with c_m2:
            days = np.linspace(0, target_time * 1.5, 100)
            S_th = [hansbo_consolidation(z_suivi_data['ch'], z_suivi_data['spacing'], t) * S_max_theorique for t in days]
            
            fig_suivi = go.Figure()
            fig_suivi.add_trace(go.Scatter(x=days, y=S_th, mode='lines', line=dict(color='blue', dash='dash'), name='Design'))
            fig_suivi.add_trace(go.Scatter(x=e_mon['Jour'], y=e_mon['Relevé (m)'], mode='markers+lines', line=dict(color='red', width=3), marker=dict(size=10), name='Terrain'))
            
            if len(e_mon) >= 3:
                s_ult, _, _, _, _ = calculate_asaoka(e_mon['Jour'].values, e_mon['Relevé (m)'].values, 15)
                if s_ult:
                    fig_suivi.add_hline(y=s_ult, line_color="orange", annotation_text=f"Asaoka ({s_ult:.2f}m)")
                    if s_ult > S_max_theorique * 1.15: st.error(f"🚨 DÉVIATION : Asaoka ({s_ult:.2f}m) dépasse le design ({S_max_theorique:.2f}m).")
            
            fig_suivi.add_hline(y=S_max_theorique, line_color="blue", annotation_text=f"S_ult ({S_max_theorique:.2f}m)")
            fig_suivi.update_layout(xaxis_title="Jours", yaxis_title="Tassement (m)", height=400)
            st.plotly_chart(fig_suivi, use_container_width=True)
            
            e_lifts['Cumul'] = e_lifts['Levée_m'].cumsum()
            f_ex = go.Figure(go.Scatter(x=e_lifts['Jour'], y=e_lifts['Cumul'], mode='lines+markers', line_shape='hv', line=dict(color='orange', width=4)))
            f_ex.add_hline(y=H_act_requis, line_dash="dash", line_color="red", annotation_text=f"Cible ({H_act_requis:.2f} m)")
            f_ex.update_layout(xaxis_title="Jours", yaxis_title="Hauteur (m)", height=250)
            st.plotly_chart(f_ex, use_container_width=True)

    # --- ONGLET 5 : RISQUES (MUDWAVE & FLUAGE) ---
    with t_risk:
        st.subheader("Bilan des Risques par Zone")
        for idx, res in d['results'].iterrows():
            st.markdown(f"**Zone {int(res['Zone'])}**")
            c_r1, c_r2 = st.columns(2)
            with c_r1:
                st.write(f"Fluage anticipé : {res['S_sec']:.3f} m")
                if res['S_sec'] > 0.15: st.warning("Fluage long terme significatif.")
            with c_r2:
                st.write(f"FS Mudwave : {res['FS_Mudwave']:.2f}")
                if res['FS_Mudwave'] < 1.3: st.error("Risque de rupture. Levées progressives obligatoires.")
            st.markdown("---")
