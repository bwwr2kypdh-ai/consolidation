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
st.set_page_config(layout="wide", page_title="Port Surcharge SIG MASTER PRO")
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


# ==========================================
# 3. MOTEUR MATHÉMATIQUE COMPLET
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
# 4. SIDEBAR : CONFIGURATION & SAUVEGARDE
# ==========================================
st.session_state["lang"] = st.sidebar.radio("🌐 Language", ["Français", "English"])
if st.sidebar.button(tr("Se déconnecter 🚪", "Logout")): st.session_state["authenticated"] = False; st.rerun()

st.sidebar.markdown("---")
with st.sidebar.expander(tr("💾 Sauvegarder / Charger Projet", "💾 Save / Load Project")):
    uploaded_file = st.file_uploader(tr("Charger .json", "Load .json"), type="json")
    if uploaded_file is not None:
        try:
            saved = json.load(uploaded_file)
            st.session_state['project_data'] = saved.get('project_data')
            if st.session_state['project_data']:
                if st.session_state['project_data'].get('mnt'):
                    st.session_state['project_data']['mnt'] = pd.read_json(st.session_state['project_data']['mnt'])
                    st.session_state['cached_mnt'] = st.session_state['project_data']['mnt']
                if st.session_state['project_data'].get('results'):
                    st.session_state['project_data']['results'] = pd.read_json(st.session_state['project_data']['results'])
                if st.session_state['project_data'].get('pvds'):
                    st.session_state['project_data']['pvds'] = pd.read_json(st.session_state['project_data']['pvds'])
            st.success("OK")
        except Exception as e: st.error(f"Erreur JSON: {e}")
    
    if st.session_state['project_data']:
        out = st.session_state['project_data'].copy()
        if isinstance(out.get('mnt'), pd.DataFrame): out['mnt'] = out['mnt'].to_json()
        if isinstance(out.get('results'), pd.DataFrame): out['results'] = out['results'].to_json()
        if isinstance(out.get('pvds'), pd.DataFrame): out['pvds'] = out['pvds'].to_json()
        st.download_button(tr("📥 Exporter", "📥 Export"), data=json.dumps({'project_data': out}), file_name="master_project.json")

st.sidebar.markdown("---")
st.sidebar.header(tr("🏗️ Niveaux & Charges", "🏗️ Levels & Loads"))
z_target_final = st.sidebar.number_input(tr("Altitude Finale (Z projet) [m]", "Target Elevation [m]"), value=4.5)
dead_load_def = st.sidebar.number_input(tr("Charge Permanente [kPa]", "Dead Load"), value=20.0)
live_load_def = st.sidebar.number_input(tr("Exploitation [kPa]", "Live Load"), value=80.0)
gamma_fill = st.sidebar.number_input(tr("Densité Remblai [kN/m³]", "Fill Density"), value=19.0)
surcharge_ratio = st.sidebar.slider("Surcharge Ratio", 1.0, 3.0, 1.25)
design_life = st.sidebar.number_input(tr("Durée de vie [Années]", "Design Life"), value=30)

st.sidebar.header(tr("🗺️ Topographie API", "🗺️ DEM API"))
api_choice = st.sidebar.selectbox("Source", ["Open-Meteo", "Google API", "Fichier CSV"])
api_key = st.sidebar.text_input("Key", type="password") if "Google" in api_choice else ""
uploaded_mnt = st.sidebar.file_uploader("Import DEM CSV", type=['csv']) if "CSV" in api_choice else None

# ==========================================
# 5. CARTE DE SAISIE & ZONAGE
# ==========================================
st.title(tr("⚓ Port Terminal - SIG Géotechnique MASTER", "⚓ Port Terminal - PRO GIS MASTER"))
col_map, col_zones = st.columns([2, 1])

with col_map:
    map_style = st.radio(tr("Vue :", "View :"), ["Satellite", "Plan", "OSM"], horizontal=True)
    tiles = {'Satellite': 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', 'Plan': 'CartoDB Positron', 'OSM': 'OpenStreetMap'}
    m = folium.Map(location=st.session_state['map_center'], zoom_start=15, tiles=tiles[map_style], attr='Esri')
    Draw(export=True).add_to(m)
    output = st_folium(m, width=800, height=500, key="input_map")

drawn_polygons = []
if output and output.get("all_drawings"):
    drawn_polygons = [d["geometry"]["coordinates"][0] for d in output["all_drawings"]]

zones_params = []
with col_zones:
    st.subheader(tr("🗜️ Stratigraphie", "🗜️ Stratigraphy"))
    if not drawn_polygons: st.info(tr("Dessinez des zones sur la carte.", "Draw polygons."))
    for i, poly in enumerate(drawn_polygons):
        with st.expander(f"🔴 Zone {i+1}", expanded=(i==0)):
            h_c = st.number_input(tr("Épaisseur Argile (m)", "Clay Thickness"), value=8.0, key=f"h_{i}")
            cc = st.number_input("Cc (Compression)", value=0.45, key=f"cc_{i}")
            cr = st.number_input("Cr (Recompression)", value=0.05, key=f"cr_{i}")
            e0 = st.number_input("e0", value=1.20, key=f"e0_{i}")
            s0 = st.number_input("σ'0 [kPa]", value=40.0, key=f"s0_{i}")
            sc = st.number_input("σ'c [kPa]", value=45.0, key=f"sc_{i}")
            su = st.number_input("Su [kPa]", value=18.0, key=f"su_{i}")
            qt = st.number_input("qt [MPa]", value=0.60, key=f"qt_{i}")
            alpha = st.number_input("α_M", value=4.0, key=f"a_{i}")
            n60 = st.number_input("N60", value=3.0, key=f"n60_{i}")
            f2 = st.number_input("f2 [kPa]", value=500.0, key=f"f2_{i}")
            ca = st.number_input("C_alpha", value=0.015, format="%.3f", key=f"ca_{i}")
            ch = st.number_input("c_h [m²/yr]", value=2.0, key=f"ch_{i}")
            sp = st.number_input("Spacing PVD (m)", value=1.2, key=f"sp_{i}")
            zones_params.append({'id': i+1, 'coords': poly, 'H': h_c, 'Cc': cc, 'Cr': cr, 'e0': e0, 'sig_0': s0, 'sig_c': sc, 'Su': su, 'qt': qt, 'alpha': alpha, 'N60': n60, 'f2': f2, 'C_alpha': ca, 'ch': ch, 'spacing': sp})

# ==========================================
# 6. DOUBLE MOTEUR : COLLECTE VS CALCULS
# ==========================================
st.markdown("---")
bt1, bt2, _ = st.columns([1, 1, 4])
with bt1: 
    btn_api = st.button("🚀 1. COLLECTER MNT (API)", use_container_width=True, type="primary", help="Scanne le terrain. À faire une seule fois.")
with bt2: 
    btn_calc = st.button("🔄 2. CALCULER / MAJ ITÉRATIONS", use_container_width=True, help="Recalcule les tassements sans toucher aux API.")

# ÉTAPE 1 : COLLECTE
if btn_api:
    with st.spinner("Appel API Topographique en cours (Par lots de 50)..."):
        if "CSV" in api_choice and uploaded_mnt:
            try:
                df_m = pd.read_csv(uploaded_mnt)
                col_z = [c for c in df_m.columns if 'Z' in c.upper() or 'ELEV' in c.upper()][0]
                df_m = df_m.rename(columns={col_z: 'Z'})
                st.session_state['cached_mnt'] = df_m
                st.success("CSV Chargé")
            except Exception as e: st.error(f"Erreur CSV: {e}")
        else:
            res_m = 15.0 / 111000.0
            all_pts = []
            for z in zones_params:
                poly_obj = Polygon(z['coords'])
                minx, miny, maxx, maxy = poly_obj.bounds
                for lt in np.arange(miny, maxy + res_m, res_m):
                    for ln in np.arange(minx, maxx + res_m, res_m):
                        if poly_obj.contains(Point(ln, lt)): all_pts.append((lt, ln))
            
            if all_pts:
                elevs = []
                chunk_size = 50 # <-- CORRECTION : Taille des lots sécurisée
                
                # BOUCLE DE REQUÊTES PAR LOTS
                if "Google" in api_choice:
                    if not api_key: st.error("Clé API Google requise.")
                    else:
                        for i in range(0, len(all_pts), chunk_size):
                            chunk = all_pts[i:i+chunk_size]
                            locs = "|".join([f"{lt},{ln}" for lt, ln in chunk])
                            try:
                                r = requests.get(f"https://maps.googleapis.com/maps/api/elevation/json?locations={locs}&key={api_key}").json()
                                if r.get('status') == 'OK': elevs.extend([res['elevation'] for res in r['results']])
                                else: elevs.extend([2.5]*len(chunk)) # Sécurité
                            except: elevs.extend([2.5]*len(chunk))
                            time.sleep(0.1)
                            
                elif "Open-Meteo" in api_choice:
                    for i in range(0, len(all_pts), chunk_size):
                        chunk = all_pts[i:i+chunk_size]
                        lats_str = ",".join([str(p[0]) for p in chunk])
                        lons_str = ",".join([str(p[1]) for p in chunk])
                        try:
                            response = requests.get(f"https://api.open-meteo.com/v1/elevation?latitude={lats_str}&longitude={lons_str}")
                            if response.status_code == 200: # <-- CORRECTION : Vérifie si c'est pas une page d'erreur
                                data = response.json()
                                elevs.extend(data.get('elevation', [2.5]*len(chunk)))
                            else:
                                elevs.extend([2.5]*len(chunk)) # Sécurité
                        except:
                            elevs.extend([2.5]*len(chunk)) # Sécurité
                        time.sleep(0.1) # Évite de se faire bloquer pour spam
                
                # STOCKAGE
                if elevs and len(elevs) == len(all_pts):
                    st.session_state['cached_mnt'] = pd.DataFrame({'Lat': [p[0] for p in all_pts], 'Lon': [p[1] for p in all_pts], 'Z': elevs})
                    st.success(tr("Topographie enregistrée !", "DEM Saved !"))
                else:
                    st.error("Échec API : Données incomplètes.")

# ÉTAPE 2 : CALCULS
if btn_calc or (btn_api and st.session_state['cached_mnt'] is not None):
    if not zones_params or st.session_state['cached_mnt'] is None:
        st.error("Error: Dessinez d'abord et collectez le MNT.")
    else:
        results, all_pvds = [], pd.DataFrame()
        mnt = st.session_state['cached_mnt']
        for z in zones_params:
            poly_obj = Polygon(z['coords'])
            mask = mnt.apply(lambda row: poly_obj.contains(Point(row['Lon'], row['Lat'])), axis=1)
            z_nat = mnt[mask]['Z'].mean() if not mnt[mask].empty else 0.0
            
            delta_z = z_target_final - z_nat
            q_remblai = max(0, delta_z * gamma_fill)
            q_exploitation = dead_load_def + live_load_def
            q_surcharge = (q_remblai + q_exploitation) * surcharge_ratio
            
            s1 = calc_settlement_oedometer(z, q_surcharge)
            s2 = calc_settlement_cptu(z, q_surcharge)
            s3 = calc_settlement_spt(z, q_surcharge)
            s_max = max(s1, s2, s3)
            
            h_fill_total = max(0, delta_z) + s_max
            vol_tot = (poly_obj.area * (111000**2) * math.cos(math.radians(poly_obj.centroid.y))) * h_fill_total
            
            df_p = generate_pvd_grid(z['coords'], z['spacing']); df_p['Zone'] = z['id']
            all_pvds = pd.concat([all_pvds, df_p])
            
            results.append({
                'Zone': z['id'], 'Z_nat': z_nat, 'Delta_Z': delta_z, 'S_max': s_max, 
                'Vol': vol_tot, 'FS': (5.14 * z['Su']) / (gamma_fill * h_fill_total) if h_fill_total > 0 else 999, 
                'H_fill': h_fill_total, 'S_oedo': s1, 'S_cpt': s2, 'S_spt': s3
            })
        st.session_state['project_data'] = {'results': pd.DataFrame(results), 'pvds': all_pvds, 'mnt': mnt, 'zones': zones_params}
        st.success("Calculs Itératifs OK.")

# ==========================================
# 7. DASHBOARD MULTI-ONGLETS
# ==========================================
if st.session_state['project_data']:
    d = st.session_state['project_data']
    tabs = st.tabs([tr("🗺️ Topographie", "🗺️ Topography"), tr("⚠️ Risques & Tassements", "⚠️ Risks"), tr("📍 Implantation PVD", "📍 PVD Layout"), tr("📉 Suivi Asaoka", "📉 Monitoring")])

    with tabs[0]:
        st.download_button("Export MNT CSV", d['mnt'].to_csv(index=False).encode('utf-8'), "site.csv")
        c1, c2 = st.columns(2)
        try:
            triang = tri.Triangulation(d['mnt']['Lon'], d['mnt']['Lat'])
            with c1:
                fig, ax = plt.subplots(); ax.tricontourf(triang, d['mnt']['Z'], levels=15, cmap="terrain"); st.pyplot(fig); plt.close()
            with c2:
                m_mnt = folium.Map(location=[d['mnt']['Lat'].mean(), d['mnt']['Lon'].mean()], zoom_start=16, tiles='CartoDB Positron')
                # AFFICHAGE PURIFIÉ : Pas de points bleus de fond, juste le texte centré.
                lats_g, lons_g = sorted(d['mnt']['Lat'].unique())[::4], sorted(d['mnt']['Lon'].unique())[::4]
                df_txt = d['mnt'][d['mnt']['Lat'].isin(lats_g) & d['mnt']['Lon'].isin(lons_g)]
                for _, r in df_txt.iterrows():
                    folium.Marker([r['Lat'], r['Lon']], icon=folium.DivIcon(html=f'<div style="font-size:10px; color:darkred; font-weight:bold; transform:translate(-50%,-50%); text-shadow: 1px 1px white, -1px -1px white;">{r["Z"]:.1f}</div>')).add_to(m_mnt)
                st_folium(m_mnt, width=600, height=400, key="mnt_m")
        except: st.warning("Contours Error")

# --- TAB 2: RISKS & SETTLEMENTS ---
    with tabs[1]:
        st.dataframe(d['results'], use_container_width=True)
        for idx, row in d['results'].iterrows():
            with st.container():
                st.markdown(f"**Analyse Zone {int(row['Zone'])}**")
                
                # --- CORRECTION : SÉCURITÉ DE RÉTROCOMPATIBILITÉ ---
                s_oedo = row.get('S_oedo', row.get('S_max', 0))
                s_cpt = row.get('S_cpt', row.get('S_max', 0))
                s_spt = row.get('S_spt', row.get('S_max', 0))
                # On cherche 'FS', et s'il n'existe pas, on cherche l'ancien 'FS_Mudwave'
                fs_val = row.get('FS', row.get('FS_Mudwave', 999.0)) 
                
                c_1, c_2 = st.columns(2)
                with c_1:
                    fig_c = go.Figure(data=[
                        go.Bar(name='Oedomètre', x=['Méthodes'], y=[s_oedo]), 
                        go.Bar(name='CPTu', x=['Méthodes'], y=[s_cpt]), 
                        go.Bar(name='SPT', x=['Méthodes'], y=[s_spt])
                    ])
                    fig_c.update_layout(height=250, margin=dict(t=30, b=0))
                    st.plotly_chart(fig_c, use_container_width=True)
                    
                with c_2:
                    if fs_val < 1.3:
                        st.error(f"🚨 RISQUE MUDWAVE ! FS = {fs_val:.2f}")
                        st.info(f"💡 Conseil : Effectuer une première levée de {(5.14*d['zones'][idx]['Su'])/(gamma_fill*1.3):.2f}m maximum.")
                    else: 
                        st.success(f"✅ FS Mudwave Stable ({fs_val:.2f})")
                st.markdown("---")

    with tabs[2]:
        tot = len(d['pvds'])
        st.metric("Total PVD", f"{tot:,.0f}")
        st.download_button("Export PVD GPS", d['pvds'].to_csv(index=False).encode('utf-8'), "pvd.csv")
        
        # CORRECTION MOIRÉ ET BLOC BLEU : Recalcul Mathématique de la grille d'affichage
        fig_p = go.Figure()
        for z in d['zones']:
            # Contour
            x, y = Polygon(z['coords']).exterior.xy
            fig_p.add_trace(go.Scattermapbox(lat=list(y), lon=list(x), mode='lines', line=dict(width=3, color='red'), name=f"Zone {z['id']}"))
            
            # Points PVD
            df_zone_pvd = d['pvds'][d['pvds']['Zone'] == z['id']]
            if len(df_zone_pvd) > 1500:
                # Génération d'une nouvelle grille aérée (Évite le moiré d'un iloc)
                vis_spacing = z['spacing'] * math.sqrt(len(df_zone_pvd) / 1500.0)
                df_vis = generate_pvd_grid(z['coords'], vis_spacing)
            else:
                df_vis = df_zone_pvd
                
            if not df_vis.empty:
                fig_p.add_trace(go.Scattermapbox(lat=df_vis['Lat'], lon=df_vis['Lon'], mode='markers', marker=dict(size=4, color='blue', opacity=0.7), name=f"PVDs Zone {z['id']}"))
                
        fig_p.update_layout(mapbox_style="carto-positron", mapbox_center={"lat": d['mnt']['Lat'].mean(), "lon": d['mnt']['Lon'].mean()}, mapbox_zoom=15, height=500)
        st.plotly_chart(fig_p, use_container_width=True)

    # --- TAB 4: MONITORING & CROSS SECTIONS ---
    with tabs[3]:
        z_sel = st.selectbox("Sélection Zone", [f"Zone {z['id']}" for z in d['zones']])
        idx = int(z_sel.split(" ")[1]) - 1
        z_inf, r_inf = d['zones'][idx], d['results'].iloc[idx]
        
        # --- SÉCURITÉ ANTI-PLANTAGE POUR LES ANCIENS FICHIERS JSON ---
        h_fill_val = r_inf.get('H_fill', r_inf.get('Fill_H', 0.0))
        s_max_val = r_inf.get('S_max', 0.0)
        
        c_m1, c_m2 = st.columns([1, 2])
        with c_m1:
            st.write("**Coupe Stratigraphique**")
            f_cp = go.Figure()
            f_cp.add_trace(go.Scatter(x=[0,100,100,0], y=[0,0,-z_inf['H'],-z_inf['H']], fill='toself', name='Argile', fillcolor='brown'))
            f_cp.add_trace(go.Scatter(x=[10,90,80,20], y=[0,0,h_fill_val,h_fill_val], fill='toself', name='Remblai', fillcolor='orange'))
            f_cp.update_layout(height=250, margin=dict(t=20, b=20)); st.plotly_chart(f_cp, use_container_width=True)
            
            df_mon = st.data_editor(pd.DataFrame({'Jour': [0, 30, 60, 90], 'Tassement_m': [0.0, 0.2, 0.45, 0.6]}), num_rows="dynamic")
        
        with c_m2:
            st.write("**Monitoring : Design vs Terrain**")
            days = np.linspace(0, 180 * 1.5, 100)
            S_th = [hansbo_consolidation(z_inf['ch'], z_inf['spacing'], t) * s_max_val for t in days]
            f_suivi = go.Figure()
            f_suivi.add_trace(go.Scatter(x=days, y=S_th, name='Théorique', line=dict(dash='dash', color='blue')))
            f_suivi.add_trace(go.Scatter(x=df_mon['Jour'], y=df_mon['Tassement_m'], name='Terrain', mode='markers+lines', marker=dict(color='red', size=10)))
            
            if len(df_mon) >= 3:
                s_ult, _, _, _, _ = calculate_asaoka(df_mon['Jour'].values, df_mon['Tassement_m'].values, 15)
                if s_ult:
                    f_suivi.add_hline(y=s_ult, line_color="orange", annotation_text=f"Asaoka ({s_ult:.2f}m)")
                    if s_ult > s_max_val * 1.15: st.error("🚨 DÉVIATION : Asaoka dépasse le design.")
            f_suivi.add_hline(y=s_max_val, line_color="blue", annotation_text=f"S_ult Théorique ({s_max_val:.2f}m)")
            f_suivi.update_layout(height=450); st.plotly_chart(f_suivi, use_container_width=True)
