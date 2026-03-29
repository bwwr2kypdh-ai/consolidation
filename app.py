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
# 1. CONFIGURATION & STATE
# ==========================================
st.set_page_config(layout="wide", page_title="Port Surcharge SIG PRO")
st.markdown("<style>@media print { .stSidebar {display: none !important;} }</style>", unsafe_allow_html=True)

if "lang" not in st.session_state: st.session_state["lang"] = "Français"
def tr(fr, en): return fr if st.session_state["lang"] == "Français" else en

if "authenticated" not in st.session_state: st.session_state["authenticated"] = False
if 'map_center' not in st.session_state: st.session_state['map_center'] = [43.2965, 5.3698]
if 'cached_mnt' not in st.session_state: st.session_state['cached_mnt'] = None
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
# 3. MOTEUR MATHÉMATIQUE CONSOLDIDÉ
# ==========================================
def calc_settlement_oedometer(layer, delta_sigma):
    if delta_sigma <= 0: return 0.0
    fs = layer['sig_0'] + delta_sigma
    if fs <= layer['sig_c']: 
        return layer['H'] * (layer['Cr'] / (1 + layer['e0'])) * math.log10(fs / layer['sig_0'])
    else: 
        return (layer['H'] * (layer['Cr'] / (1 + layer['e0'])) * math.log10(layer['sig_c'] / layer['sig_0'])) + \
               (layer['H'] * (layer['Cc'] / (1 + layer['e0'])) * math.log10(fs / layer['sig_c']))

def calc_settlement_cptu(layer, delta_sigma):
    M = max(1000, layer['alpha'] * ((layer['qt'] * 1000) - layer['sig_v0']))
    return (delta_sigma / M) * layer['H']

def calc_settlement_spt(layer, delta_sigma):
    M = max(1000, layer['f2'] * layer['N60'])
    return (delta_sigma / M) * layer['H']

def calc_secondary_compression(C_alpha, H, t_days, t_years):
    t1 = max(1, t_days) / 365.25; t2 = t1 + t_years
    return (C_alpha * H * math.log10(t2 / t1))

def generate_pvd_grid(polygon_coords, spacing):
    poly = Polygon(polygon_coords)
    minx, miny, maxx, maxy = poly.bounds
    lat_to_m, lon_to_m = 111000, 111000 * math.cos(math.radians(miny))
    dx_deg, dy_deg = spacing / lon_to_m, (spacing * math.sqrt(3) / 2) / lat_to_m
    pvds = []
    for i, y in enumerate(np.arange(miny, maxy + dy_deg, dy_deg)):
        offset = (dx_deg / 2) if i % 2 != 0 else 0
        for x in np.arange(minx, maxx + dx_deg, dx_deg):
            pt = Point(x + offset, y)
            if poly.contains(pt): pvds.append({'Lon': pt.x, 'Lat': pt.y})
    return pd.DataFrame(pvds)

def hansbo_consolidation(ch_m2_yr, spacing, t_days):
    if t_days <= 0: return 0.0
    Tr = (ch_m2_yr / 365.25 * t_days) / ((1.05 * spacing)**2)
    dw, D = 0.052, 1.05 * spacing
    Fn = math.log(D/dw) - 0.75
    try: return max(0.0, min(1.0 - math.exp((-8.0 * Tr) / Fn), 1.0))
    except: return 1.0

def calculate_asaoka(times, settlements, delta_t=15):
    if len(times) < 3: return None, None, None, None, None
    t_interp = np.arange(min(times), max(times) + delta_t, delta_t)
    s_interp = np.interp(t_interp, times, settlements)
    s_n1, s_n = s_interp[:-1], s_interp[1:]
    coef = np.polyfit(s_n1, s_n, 1)
    if coef[0] >= 1.0 or coef[0] <= 0: return None, None, None, None, None 
    return coef[1] / (1 - coef[0]), coef[1], coef[0], s_n1, s_n

# ==========================================
# 4. SIDEBAR : LANG, SAVE/LOAD, CONFIG
# ==========================================
st.session_state["lang"] = st.sidebar.radio("🌐 Langue", ["Français", "English"])
if st.sidebar.button(tr("Se déconnecter 🚪", "Logout")): st.session_state["authenticated"] = False; st.rerun()

st.sidebar.markdown("---")
with st.sidebar.expander(tr("💾 Sauvegarder / Charger (JSON)", "💾 Save / Load (JSON)")):
    uploaded_file = st.file_uploader(tr("Charger projet", "Load project"), type="json")
    if uploaded_file is not None:
        try:
            saved = json.load(uploaded_file)
            st.session_state['project_data'] = saved.get('project_data')
            if st.session_state['project_data']:
                st.session_state['project_data']['mnt'] = pd.read_json(st.session_state['project_data']['mnt'])
                st.session_state['cached_mnt'] = st.session_state['project_data']['mnt']
            st.success(tr("Chargé !", "Loaded !"))
        except: st.error("Erreur de chargement.")

    if st.session_state['project_data']:
        out = st.session_state['project_data'].copy()
        out['mnt'] = out['mnt'].to_json() if isinstance(out['mnt'], pd.DataFrame) else None
        st.download_button(tr("📥 Exporter", "📥 Export"), data=json.dumps({'project_data': out}), file_name="projet.json")

st.sidebar.markdown("---")
st.sidebar.header(tr("🏗️ Configuration Projet", "🏗️ Project Config"))
z_target_final = st.sidebar.number_input(tr("Niveau Fini Visé (Z projet) [m]", "Target Elevation [m]"), value=4.5)
dead_load_def = st.sidebar.number_input(tr("Charge Chaussée [kPa]", "Pavement Load"), value=20.0)
live_load_def = st.sidebar.number_input(tr("Exploitation [kPa]", "Live Load"), value=80.0)
gamma_fill = st.sidebar.number_input(tr("Densité Remblai [kN/m³]", "Fill Density"), value=19.0)
surcharge_ratio = st.sidebar.slider("Surcharge Ratio", 1.0, 2.0, 1.25)
design_life = st.sidebar.number_input(tr("Durée de vie [Ans]", "Lifespan"), value=30)

st.sidebar.header(tr("🗺️ Topographie API", "🗺️ DEM API"))
api_choice = st.sidebar.selectbox("Source", ["Open-Meteo", "Google API", "CSV"])
api_key = st.sidebar.text_input("Key", type="password") if "Google" in api_choice else ""
uploaded_mnt = st.sidebar.file_uploader("Import DEM CSV", type=['csv']) if "CSV" in api_choice else None

# ==========================================
# 5. CARTE DE SAISIE & ZONAGE DYNAMIQUE
# ==========================================
st.title(tr("⚓ Port Terminal - SIG Itératif MASTER", "⚓ Port Terminal - GIS Master"))

col_map, col_action = st.columns([2, 1])

with col_map:
    map_style = st.radio(tr("Vue :", "View :"), ["Satellite (Esri)", "Plan", "OSM"], horizontal=True)
    tiles = {'Satellite (Esri)': 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', 'Plan': 'CartoDB Positron', 'OSM': 'OpenStreetMap'}
    m = folium.Map(location=st.session_state['map_center'], zoom_start=15, tiles=tiles[map_style], attr='Esri')
    Draw(export=True).add_to(m)
    output = st_folium(m, width=800, height=500, key="input_map")

drawn_polygons = []
if output and output.get("all_drawings"):
    drawn_polygons = [d["geometry"]["coordinates"][0] for d in output["all_drawings"]]

zones_data = []
with col_action:
    st.subheader(tr("🗜️ Stratigraphie par Zone", "🗜️ Zoning"))
    if not drawn_polygons: st.info(tr("Dessinez des polygones.", "Draw polygons."))
    for i, poly in enumerate(drawn_polygons):
        with st.expander(f"Zone {i+1}", expanded=(i==0)):
            h_clay = st.number_input(tr("Épaisseur (m)", "Thickness"), value=8.0, key=f"H_{i}")
            su = st.number_input("Su [kPa]", value=18.0, key=f"Su_{i}")
            cc = st.number_input("Cc", value=0.45, key=f"Cc_{i}")
            spacing = st.number_input("PVD Spacing (m)", value=1.2, key=f"sp_{i}")
            zones_data.append({
                'id': i+1, 'coords': poly, 'H': h_clay, 'Su': su, 'Cc': cc, 'spacing': spacing,
                'e0': 1.2, 'Cr': 0.05, 'sig_0': 45.0, 'sig_c': 50.0, 'ch': 2.0, 'C_alpha': 0.015,
                'alpha': 4.0, 'qt': 0.6, 'sig_v0': 50.0, 'N60': 3.0, 'f2': 500.0
            })

# ==========================================
# 6. MOTEUR API ET CALCULS (DÉCOUPLÉS)
# ==========================================
st.markdown("---")
c1, c2, c3 = st.columns([1, 1, 4])
with c1: btn_api = st.button("🚀 1. COLLECTER API MNT", use_container_width=True, type="primary")
with c2: btn_calc = st.button("🔄 2. CALCULER ITÉRATIONS", use_container_width=True)

if btn_api:
    with st.spinner(tr("Scan topographique...", "Scanning topo...")):
        if "CSV" in api_choice and uploaded_mnt:
            df_m = pd.read_csv(uploaded_mnt)
            df_m.columns = ['Lat', 'Lon', 'Z']
            st.session_state['cached_mnt'] = df_m
        else:
            all_coords = [p for z in zones_data for p in z['coords']]
            if all_coords:
                min_lon, max_lon = min(p[0] for p in all_coords), max(p[0] for p in all_coords)
                min_lat, max_lat = min(p[1] for p in all_coords), max(p[1] for p in all_coords)
                res = 15.0 / 111000.0
                mnt_pts = [(lt, ln) for lt in np.arange(min_lat, max_lat + res, res) for ln in np.arange(min_lon, max_lon + res, res) if any(Polygon(z['coords']).contains(Point(ln, lt)) for z in zones_data)]
                elevs = []
                if "Open-Meteo" in api_choice:
                    r = requests.get(f"https://api.open-meteo.com/v1/elevation?latitude={','.join([str(p[0]) for p in mnt_pts])}&longitude={','.join([str(p[1]) for p in mnt_pts])}").json()
                    elevs = r.get('elevation', [2.0]*len(mnt_pts))
                st.session_state['cached_mnt'] = pd.DataFrame({'Lat': [p[0] for p in mnt_pts], 'Lon': [p[1] for p in mnt_pts], 'Z': elevs})
                st.success(tr("MNT en cache.", "DEM cached."))

if btn_calc or (btn_api and st.session_state['cached_mnt'] is not None):
    if not zones_data or st.session_state['cached_mnt'] is None:
        st.error(tr("Dessinez d'abord et collectez le MNT.", "Draw first and collect DEM."))
    else:
        with st.spinner(tr("Calculs géotechniques...", "Calculations...")):
            results, all_pvds = [], pd.DataFrame()
            mnt = st.session_state['cached_mnt']
            for z in zones_data:
                poly = Polygon(z['coords'])
                mask = mnt.apply(lambda row: poly.contains(Point(row['Lon'], row['Lat'])), axis=1)
                z_mnt = mnt[mask]
                z_nat_avg = z_mnt['Z'].mean() if not z_mnt.empty else 2.0
                
                delta_z = z_target_final - z_nat_avg
                q_remblai = max(0, delta_z * gamma_fill)
                q_total = q_remblai + dead_load_def + live_load_def
                q_surcharge = q_total * surcharge_ratio
                
                s_oedo = calc_settlement_oedometer(z, q_surcharge)
                s_cptu = calc_settlement_cptu(z, q_surcharge)
                s_spt = calc_settlement_spt(z, q_surcharge)
                s_max = max(s_oedo, s_cptu, s_spt)
                s_sec = calc_secondary_compression(z['C_alpha'], z['H'], 180, design_life)
                
                area = poly.area * (111000**2) * math.cos(math.radians(poly.centroid.y))
                h_terre = max(0, delta_z) + s_max
                vol = area * h_terre
                
                fs = (5.14 * z['Su']) / (gamma_fill * h_terre) if h_terre > 0 else 999
                
                df_p = generate_pvd_grid(z['coords'], z['spacing'])
                df_p['Zone'] = z['id']
                all_pvds = pd.concat([all_pvds, df_p])
                
                results.append({'Zone': z['id'], 'S_max': s_max, 'S_sec': s_sec, 'Vol': vol, 'FS': fs, 'Area': area, 'H_total': h_terre, 'Z_nat': z_nat_avg})
            
            st.session_state['project_data'] = {'results': pd.DataFrame(results), 'pvds': all_pvds, 'mnt': mnt, 'zones': zones_data}
            st.success(tr("Itération terminée.", "Iteration done."))

# ==========================================
# 7. DASHBOARD RÉSULTATS (TOUS LES ONGLETS)
# ==========================================
if st.session_state['project_data']:
    d = st.session_state['project_data']
    tabs = st.tabs([tr("🗺️ Topographie", "🗺️ Topography"), tr("⚠️ Risques & Tassements", "⚠️ Risks"), tr("📍 Implantation PVD", "📍 PVD Layout"), tr("📈 Suivi Asaoka", "📉 Monitoring")])

    with tabs[0]:
        st.download_button("Export MNT CSV", d['mnt'].to_csv(index=False).encode('utf-8'), "mnt.csv")
        c1, c2 = st.columns(2)
        triang = tri.Triangulation(d['mnt']['Lon'], d['mnt']['Lat'])
        with c1:
            fig, ax = plt.subplots(); ax.tricontourf(triang, d['mnt']['Z'], levels=15, cmap="terrain")
            st.pyplot(fig); plt.close()
        with c2:
            # Grille MNT parfaite Centrée
            m_mnt = folium.Map(location=[d['mnt']['Lat'].mean(), d['mnt']['Lon'].mean()], zoom_start=16, tiles='CartoDB Positron')
            lats_g = sorted(d['mnt']['Lat'].unique())[::4]; lons_g = sorted(d['mnt']['Lon'].unique())[::4]
            df_t = d['mnt'][d['mnt']['Lat'].isin(lats_g) & d['mnt']['Lon'].isin(lons_g)]
            for _, r in df_t.iterrows():
                folium.Marker([r['Lat'], r['Lon']], icon=folium.DivIcon(html=f'<div style="font-size:10px; color:darkred; font-weight:bold; transform:translate(-50%,-50%);">{r["Z"]:.1f}</div>')).add_to(m_mnt)
            st_folium(m_mnt, width=600, height=400, key="mnt_map")

    with tabs[1]:
        st.dataframe(d['results'])
        for idx, row in d['results'].iterrows():
            if row['FS'] < 1.3:
                st.error(f"ZONE {int(row['Zone'])} : Risque MUDWAVE ! Levée max sécurisée : {(5.14 * d['zones'][idx]['Su'])/(gamma_fill*1.3):.2f}m")

    with tabs[2]:
        tot = len(d['pvds'])
        st.write(f"Total PVD : {tot:,.0f}")
        st.download_button("Export PVD GPS", d['pvds'].to_csv(index=False).encode('utf-8'), "pvd.csv")
        # Anti-Moiré Logic
        step = max(1, tot // 2000)
        df_vis = d['pvds'].iloc[::step]
        fig_pvd = go.Figure(go.Scattermapbox(lat=df_vis['Lat'], lon=df_vis['Lon'], mode='markers', marker=dict(size=4, color='blue', opacity=0.6)))
        fig_pvd.update_layout(mapbox_style="carto-positron", mapbox_center={"lat": d['mnt']['Lat'].mean(), "lon": d['mnt']['Lon'].mean()}, mapbox_zoom=15)
        st.plotly_chart(fig_pvd, use_container_width=True)

    with tabs[3]:
        z_sel = st.selectbox("Zone", [f"Zone {z['id']}" for z in d['zones']])
        idx = int(z_sel.split(" ")[1]) - 1
        c_m1, c_m2 = st.columns([1, 2])
        with c_m1:
            df_m = st.data_editor(pd.DataFrame({'Jour': [0, 15, 30, 45, 60], 'Relevé_m': [0.0, 0.1, 0.25, 0.4, 0.55]}), num_rows="dynamic")
        with c_m2:
            days = np.linspace(0, 180 * 1.5, 100)
            S_th = [hansbo_consolidation(d['zones'][idx]['ch'], d['zones'][idx]['spacing'], t) * d['results'].iloc[idx]['S_max'] for t in days]
            f_suivi = go.Figure(); f_suivi.add_trace(go.Scatter(x=days, y=S_th, name='Design', line=dict(dash='dash')))
            f_suivi.add_trace(go.Scatter(x=df_m['Jour'], y=df_m['Relevé_m'], name='Réel', mode='markers+lines'))
            st.plotly_chart(f_suivi, use_container_width=True)
