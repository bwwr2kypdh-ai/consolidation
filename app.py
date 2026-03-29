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
# 5. SIDEBAR : SAVE/LOAD & TOPOGRAPHY
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

# ==========================================
# 6. SIDEBAR : GEOTECH PARAMS & ASSISTANTS
# ==========================================
st.sidebar.markdown("---")
st.sidebar.header(tr("🏗️ Projet & Logistique", "🏗️ Project & Logistics"))
dead_load = st.sidebar.number_input("Charge Permanente [kPa]", value=20.0, help="Poids des matériaux définitifs (Chaussée, fondation).")
live_load = st.sidebar.number_input("Exploitation [kPa]", value=80.0, help="Poids des grues, conteneurs, trafics.")
gamma_fill = st.sidebar.number_input("Densité Remblai [kN/m³]", value=19.0)

st.sidebar.markdown("---")
assist_time = st.sidebar.toggle("⏱️ Assistant Fast-Track (Optimisation Délai)", value=False, help="Activez pour forcer un délai court. L'IA calculera la hauteur de remblai supplémentaire requise.")
if assist_time:
    target_time = st.sidebar.number_input("Délai maximal imposé (Jours)", value=90, step=10)
    surcharge_ratio = 1.2 # Base for iteration
else:
    surcharge_ratio = st.sidebar.slider("Surcharge Ratio", 1.0, 2.0, 1.2)
    target_time = st.sidebar.number_input("Temps d'observation (Jours)", value=180)

design_life = st.sidebar.number_input("Durée de vie ouvrage [Années]", value=30)

# ==========================================
# 7. MAIN UI & INTERACTIVE MAP 
# ==========================================
st.title(tr("⚓ Port Terminal - SIG Géotechnique PRO", "⚓ Port Terminal - PRO Geotechnical GIS"))

map_style = st.radio(tr("Vue de la carte :", "Map View :"), ["Satellite (Esri)", "Plan (CartoDB)", "OSM"], horizontal=True)
tiles_dict = {"Satellite (Esri)": 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', "Plan (CartoDB)": 'CartoDB Positron', "OSM": 'OpenStreetMap'}
attr_dict = {"Satellite (Esri)": 'Esri', "Plan (CartoDB)": 'CartoDB', "OSM": 'OSM'}

m = folium.Map(location=st.session_state['map_center'], zoom_start=15, tiles=tiles_dict[map_style], attr=attr_dict[map_style])
Draw(export=True, draw_options={'polyline':False, 'polygon':True, 'rectangle':True, 'circle':False, 'marker':False}).add_to(m)

col_map, col_action = st.columns([2, 1])
with col_map:
    output = st_folium(m, width=800, height=500, key="input_map")

drawn_polygons = []
if output and output.get("all_drawings"):
    drawn_polygons = [d["geometry"]["coordinates"][0] for d in output["all_drawings"] if d.get("geometry", {}).get("type") in ["Polygon", "Rectangle"]]

# ==========================================
# 8. DYNAMIC STRATIGRAPHY (ZONING)
# ==========================================
zones_data = []
if len(drawn_polygons) == 0:
    st.sidebar.warning(tr("Dessinez au moins une zone sur la carte.", "Draw at least one zone on the map."))
else:
    st.sidebar.header(tr("🗜️ Stratigraphie par Zone", "🗜️ Zoning Stratigraphy"))
    for i, poly_coords in enumerate(drawn_polygons):
        with st.sidebar.expander(f"🔴 Zone {i+1} Paramètres", expanded=(i==0)):
            H = st.number_input("Épaisseur Argile (m)", value=8.0, key=f"H_{i}")
            e0 = st.number_input("e0", value=1.20, key=f"e0_{i}")
            Cc = st.number_input("Cc", value=0.45, key=f"Cc_{i}")
            Cr = st.number_input("Cr", value=0.05, key=f"Cr_{i}")
            sig_0 = st.number_input("σ'0 [kPa]", value=40.0, key=f"s0_{i}")
            sig_c = st.number_input("σ'c [kPa]", value=45.0, key=f"sc_{i}")
            qt = st.number_input("qt (CPTu) [MPa]", value=0.60, key=f"qt_{i}")
            sig_v0 = st.number_input("σv0 (Totale) [kPa]", value=50.0, key=f"sv0_{i}")
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
# 9. CALCULATION ENGINE
# ==========================================
with col_action:
    if st.button(tr("🚀 LANCER L'ANALYSE SIG", "🚀 RUN GIS ANALYSIS"), use_container_width=True, type="primary"):
        if not zones_data: st.error(tr("Aucune zone définie.", "No zones defined."))
        else:
            with st.spinner(tr("Génération Topographie & Tassements...", "Generating Topo & Settlement...")):
                final_load = dead_load + live_load
                
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
                    
                    valid_pts = []
                    for lt in lats:
                        for ln in lons:
                            pt = Point(ln, lt)
                            if any(Polygon(z['coords']).contains(pt) for z in zones_data):
                                valid_pts.append((lt, ln))
                    
                    mnt_pts = valid_pts if valid_pts else [(min_lat, min_lon)]
                    
                    elevs = []
                    if "Google" in api_choice and api_key:
                        for i in range(0, len(mnt_pts), 50):
                            locs = "|".join([f"{lt},{ln}" for lt, ln in mnt_pts[i:i+50]])
                            try:
                                r = requests.get(f"https://maps.googleapis.com/maps/api/elevation/json?locations={locs}&key={api_key}").json()
                                if r.get('status') == 'OK': elevs.extend([res['elevation'] for res in r['results']])
                            except: break; time.sleep(0.1)
                    elif "Open-Meteo" in api_choice:
                        try:
                            lats_str, lons_str = ",".join([str(p[0]) for p in mnt_pts]), ",".join([str(p[1]) for p in mnt_pts])
                            r = requests.get(f"https://api.open-meteo.com/v1/elevation?latitude={lats_str}&longitude={lons_str}").json()
                            if 'elevation' in r: elevs = r['elevation']
                        except: pass
                    
                    if elevs and len(elevs) == len(mnt_pts): df_mnt = pd.DataFrame({'Lat': [p[0] for p in mnt_pts], 'Lon': [p[1] for p in mnt_pts], 'Z': elevs})
                    else:
                        elevs = [2.0 + math.sin(lt*1000) for lt, ln in mnt_pts]
                        df_mnt = pd.DataFrame({'Lat': [p[0] for p in mnt_pts], 'Lon': [p[1] for p in mnt_pts], 'Z': elevs})

                # --- CALCULS PAR ZONE ---
                results_zones = []
                all_pvds = pd.DataFrame()
                
                for z in zones_data:
                    poly = Polygon(z['coords'])
                    area = poly.area * (111000**2) * math.cos(math.radians(poly.centroid.y))
                    
                    # FAST TRACK LOGIC
                    opt_ratio = surcharge_ratio
                    S_design_requis = max(calc_settlement_oedometer(z, final_load), calc_settlement_cptu(z, final_load), calc_settlement_spt(z, final_load))
                    
                    if assist_time:
                        U_t = hansbo_consolidation(z['ch'], z['spacing'], target_time)
                        if U_t < 0.05: U_t = 0.05 
                        while True:
                            test_load = final_load * opt_ratio
                            S_test = max(calc_settlement_oedometer(z, test_load), calc_settlement_cptu(z, test_load), calc_settlement_spt(z, test_load))
                            if (S_test * U_t) >= S_design_requis or opt_ratio > 3.0: break
                            opt_ratio += 0.05
                            
                    z_target_load = final_load * opt_ratio
                    z_base_fill = z_target_load / gamma_fill
                    
                    S_max = max(calc_settlement_oedometer(z, z_target_load), calc_settlement_cptu(z, z_target_load), calc_settlement_spt(z, z_target_load))
                    S_sec = calc_secondary_compression(z['C_alpha'], z['H'], target_time, design_life)
                    actual_fill = z_base_fill + S_max
                    vol = area * actual_fill
                    
                    q_ult = 5.14 * z['Su']
                    FS_mudwave = q_ult / (gamma_fill * actual_fill) if actual_fill > 0 else 999
                    
                    df_pvd = generate_pvd_grid(z['coords'], z['spacing'])
                    if not df_pvd.empty:
                        df_pvd['Zone'] = z['id']
                        all_pvds = pd.concat([all_pvds, df_pvd])
                    
                    results_zones.append({
                        'Zone': z['id'], 'Area': area, 'S_max': S_max, 'S_sec': S_sec, 'Opt_Ratio': opt_ratio,
                        'Fill_H': actual_fill, 'Vol': vol, 'FS_Mudwave': FS_mudwave, 'PVD_Count': len(df_pvd)
                    })
                
                st.session_state['project_data'] = {
                    'zones': zones_data, 'results': pd.DataFrame(results_zones), 
                    'pvds': all_pvds, 'mnt': df_mnt, 'assist_time_used': assist_time
                }
                st.success(tr("Analyse SIG Terminée !", "GIS Analysis Complete!"))

# ==========================================
# 10. RESULTS DASHBOARD
# ==========================================
if st.session_state['project_data'] is not None:
    d = st.session_state['project_data']
    st.markdown("---")
    
    t_risk, t_topo, t_pvd, t_suivi = st.tabs([
        tr("⚠️ Ingénierie & Sécurité", "⚠️ Engineering & Safety"), 
        tr("🗺️ Modèle MNT", "🗺️ DEM Model"), 
        tr("📍 Logistique PVD", "📍 PVD Logistics"), 
        tr("📉 Suivi & Coupes", "📉 Monitoring & Sections")
    ])
    
    # --- ONGLET 1 : INGÉNIERIE, ASSISTANTS ET RISQUES ---
    with t_risk:
        st.subheader("Bilan des Risques et Assistants d'Exécution")
        if d.get('assist_time_used', False):
            st.success(f"🚀 **Assistant Fast-Track Activé :** Pour atteindre la consolidation dans le délai imposé de **{target_time} jours**, l'IA a redimensionné les hauteurs.")

        for idx, res in d['results'].iterrows():
            z_data = d['zones'][idx]
            with st.container():
                st.markdown(f"### 📍 Zone {int(res['Zone'])}")
                c_r1, c_r2, c_r3 = st.columns(3)
                c_r1.metric("Hauteur Remblai Cible", f"{res['Fill_H']:.2f} m", f"Ratio Surcharge: {res['Opt_Ratio']:.2f}")
                c_r2.metric("Fluage Anticipé", f"{res['S_sec']:.3f} m")
                c_r3.metric("FS Mudwave", f"{res['FS_Mudwave']:.2f}", "Danger" if res['FS_Mudwave'] < 1.3 else "Sécurisé", delta_color="inverse")
                
                if res['FS_Mudwave'] < 1.3:
                    st.error("🚨 Risque de rupture au cisaillement (Mudwave).")
                    assist_mw = st.toggle(f"🛡️ Assistant : Générer Phasage Sécurisé (Zone {int(res['Zone'])})", key=f"amw_{idx}")
                    if assist_mw:
                        H_safe_1 = (5.14 * z_data['Su']) / (gamma_fill * 1.3)
                        st.info(f"**Protocole Automatisé :**\n1. **Levée 1 :** Poser maximum **{H_safe_1:.2f} m**.\n2. **Palier d'attente :** Patienter (~45j) pour la dissipation interstitielle.\n3. **Levées Suivantes :** Par passes de **{H_safe_1 * 0.8:.2f} m**.")
                st.markdown("---")

    # --- ONGLET 2 : TOPOGRAPHIE (SOUS-ECHANTILLONNEE) ---
    with t_topo:
        c1, c2 = st.columns([1, 2])
        with c1:
            st.write(tr("**Carte Interactive MNT**", "**Interactive DEM Map**"))
            if d['mnt'] is not None and not d['mnt'].empty:
                st.download_button(tr("📥 Télécharger MNT Complet (CSV)", "📥 Download Full DEM"), data=d['mnt'].to_csv(index=False).encode('utf-8'), file_name='mnt_complet.csv', mime='text/csv')
        
        with c2:
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
                    icon=folium.DivIcon(html=f'<div style="font-size: 11px; font-weight: bold; color: #8B0000; text-shadow: 1px 1px 0px white, -1px -1px 0px white, 1px -1px 0px white, -1px 1px 0px white; white-space: nowrap; transform: translate(-50%, -50%);">{row["Z"]:.1f}</div>')
                ).add_to(m_mnt)
                
            st_folium(m_mnt, width=1000, height=500, key="mnt_grid_map")

    # --- ONGLET 3 : LOGISTIQUE PVD (ANTI-MOIRE) ---
    with t_pvd:
        tot_pvd = len(d['pvds']) if not d['pvds'].empty else 0
        st.write(f"Nombre total de PVD à commander : **{tot_pvd:,.0f} unités**.")
        
        if not d['pvds'].empty:
            st.download_button(tr("📥 Télécharger Coordonnées PVD (Exécution)", "📥 Download PVD Coordinates"), data=d['pvds'].to_csv(index=False).encode('utf-8'), file_name='implantation_pvd.csv', mime='text/csv', type="primary")
            
            fig_pvd = go.Figure()
            for z in d['zones']:
                x, y = Polygon(z['coords']).exterior.xy
                fig_pvd.add_trace(go.Scattermapbox(lat=list(y), lon=list(x), mode='lines', line=dict(width=3, color='red'), name=f"Zone {z['id']}"))
                
                df_zone_pvd = d['pvds'][d['pvds']['Zone'] == z['id']]
                if len(df_zone_pvd) > 2000:
                    vis_spacing = z['spacing'] * math.sqrt(len(df_zone_pvd) / 2000)
                    df_plot_pvd = generate_pvd_grid(z['coords'], vis_spacing)
                    st.warning(f"⚠️ Zone {z['id']} : Maille affichée élargie pour fluidité. Le CSV téléchargé contient la grille exacte.")
                else: df_plot_pvd = df_zone_pvd

                if not df_plot_pvd.empty:
                    fig_pvd.add_trace(go.Scattermapbox(lat=df_plot_pvd['Lat'], lon=df_plot_pvd['Lon'], mode='markers', marker=go.scattermapbox.Marker(size=3, color='blue', opacity=0.7), name=f"PVDs Zone {z['id']}"))
            
            fig_pvd.update_layout(mapbox_style="carto-positron", mapbox_zoom=15, mapbox_center={"lat": d['pvds']['Lat'].mean(), "lon": d['pvds']['Lon'].mean()}, height=500, margin={"r":0,"t":0,"l":0,"b":0})
            st.plotly_chart(fig_pvd, use_container_width=True)

    # --- ONGLET 4 : SUIVI & COUPES ---
    with t_suivi:
        st.subheader("Monitoring (Asaoka) & Vues en Coupe")
        zone_suivi = st.selectbox("Sélectionner la Zone :", [f"Zone {z['id']}" for z in d['zones']], key="suivi_zone")
        zs_idx = int(zone_suivi.split(" ")[1]) - 1
        z_data, res_suivi = d['zones'][zs_idx], d['results'].iloc[zs_idx]
        
        c_m1, c_m2 = st.columns([1, 2])
        with c_m1:
            st.write("**Coupe Transversale**")
            fig_coupe = go.Figure()
            fig_coupe.add_trace(go.Scatter(x=[0, 100, 100, 0], y=[0, 0, -z_data['H'], -z_data['H']], fill='toself', fillcolor='saddlebrown', line=dict(color='black'), name="Argile Molle"))
            fig_coupe.add_trace(go.Scatter(x=[10, 90, 80, 20], y=[0, 0, res_suivi['Fill_H'], res_suivi['Fill_H']], fill='toself', fillcolor='orange', line=dict(color='black'), name="Surcharge"))
            fig_coupe.add_hline(y=-res_suivi['S_max'], line_dash="dash", line_color="red", annotation_text=f"Tassement Max (-{res_suivi['S_max']:.2f}m)")
            fig_coupe.update_layout(height=250, margin=dict(t=20, b=20)); st.plotly_chart(fig_coupe, use_container_width=True)
            
            st.write("**Saisie Relevés (Terrain)**")
            e_mon = st.data_editor(pd.DataFrame({'Jour': [0, 15, 30, 45, 60], 'Relevé (m)': [0.0, 0.10, 0.25, 0.40, 0.55]}), num_rows="dynamic", use_container_width=True, key=f"m_{zs_idx}").sort_values(by='Jour')
        
        with c_m2:
            st.write("**Consolidation : Design vs Réalité**")
            days = np.linspace(0, target_time * 1.5, 100) # FIXED: Using global target_time
            S_th = [hansbo_consolidation(z_data['ch'], z_data['spacing'], t) * res_suivi['S_max'] for t in days]
            
            fig_suivi = go.Figure()
            fig_suivi.add_trace(go.Scatter(x=days, y=S_th, mode='lines', line=dict(color='blue', dash='dash'), name='Design'))
            fig_suivi.add_trace(go.Scatter(x=e_mon['Jour'], y=e_mon['Relevé (m)'], mode='markers+lines', line=dict(color='red', width=3), marker=dict(size=10), name='Terrain'))
            
            if len(e_mon) >= 3:
                s_ult, _, _, _, _ = calculate_asaoka(e_mon['Jour'].values, e_mon['Relevé (m)'].values, 15)
                if s_ult:
                    fig_suivi.add_hline(y=s_ult, line_color="orange", annotation_text=f"Asaoka ({s_ult:.2f}m)")
                    if s_ult > res_suivi['S_max'] * 1.15: st.error(f"🚨 DÉVIATION : L'Asaoka réel dépasse le design de plus de 15%.")
            fig_suivi.add_hline(y=res_suivi['S_max'], line_color="blue", annotation_text=f"S_ult Théorique ({res_suivi['S_max']:.2f}m)")
            fig_suivi.update_layout(xaxis_title="Jours", yaxis_title="Tassement (m)", height=450); st.plotly_chart(fig_suivi, use_container_width=True)



permettre de maj le calcul des tassements et le model après chaque changement, sans relancer la collecte des API.... cela parait évident... aussi où choisir le niveau fini envisagé pour calculer la surchage liée au remblai ou déblai...
