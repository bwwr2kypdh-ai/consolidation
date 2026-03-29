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
# 3. MOTEUR MATHÉMATIQUE
# ==========================================
def calc_settlement_oedometer(layer, delta_sigma):
    if delta_sigma <= 0: return 0.0
    fs = layer.get('sig_0', 40.0) + delta_sigma
    sig_c, sig_0 = layer.get('sig_c', 45.0), layer.get('sig_0', 40.0)
    cr, cc, e0, H = layer.get('Cr', 0.05), layer.get('Cc', 0.45), layer.get('e0', 1.2), layer.get('H', 8.0)
    if fs <= sig_c: return H * (cr / (1 + e0)) * math.log10(fs / sig_0)
    else: return (H * (cr / (1 + e0)) * math.log10(sig_c / sig_0)) + (H * (cc / (1 + e0)) * math.log10(fs / sig_c))

def calc_settlement_cptu(layer, delta_sigma):
    M = max(1000, layer.get('alpha', 4.0) * ((layer.get('qt', 0.6) * 1000) - layer.get('sig_v0', 50.0)))
    return (delta_sigma / M) * layer.get('H', 8.0)

def calc_settlement_spt(layer, delta_sigma):
    M = max(1000, layer.get('f2', 500.0) * layer.get('N60', 3.0))
    return (delta_sigma / M) * layer.get('H', 8.0)

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
    try: return max(0.0, min(1.0 - math.exp((-8.0 * Tr) / (math.log((1.05 * spacing)/0.052) - 0.75)), 1.0))
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
# 4. SIDEBAR : CONFIGURATION & JSON
# ==========================================
st.session_state["lang"] = st.sidebar.radio("🌐 Language", ["Français", "English"])

st.sidebar.markdown("---")
with st.sidebar.expander(tr("💾 Sauvegarder / Charger Projet", "💾 Save / Load Project")):
    uploaded_file = st.file_uploader("Charger JSON", type="json")
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
            st.success("JSON Chargé. Cliquez sur MAJ Itérations.")
        except Exception as e: st.error(f"Erreur: {e}")
    
    if st.session_state['project_data']:
        out = st.session_state['project_data'].copy()
        if isinstance(out.get('mnt'), pd.DataFrame): out['mnt'] = out['mnt'].to_json()
        if isinstance(out.get('results'), pd.DataFrame): out['results'] = out['results'].to_json()
        if isinstance(out.get('pvds'), pd.DataFrame): out['pvds'] = out['pvds'].to_json()
        st.download_button("📥 Exporter JSON", data=json.dumps({'project_data': out}), file_name="master_project.json", mime="application/json")

st.sidebar.markdown("---")
st.sidebar.header(tr("🏗️ Niveaux & Charges", "🏗️ Levels & Loads"))
z_target = st.sidebar.number_input(tr("Altitude Finale (Z projet) [m]", "Target Elev [m]"), value=4.5)
dead_load = st.sidebar.number_input(tr("Charge Permanente [kPa]", "Dead Load"), value=20.0)
live_load = st.sidebar.number_input(tr("Exploitation [kPa]", "Live Load"), value=80.0)
gamma_fill = st.sidebar.number_input(tr("Densité Remblai [kN/m³]", "Fill Density"), value=19.0)
surcharge_ratio = st.sidebar.slider("Surcharge Ratio", 1.0, 3.0, 1.25)
design_life = st.sidebar.number_input(tr("Durée de vie [Années]", "Design Life"), value=30)

st.sidebar.header(tr("🗺️ Topographie API", "🗺️ DEM API"))
api_choice = st.sidebar.selectbox("Source", ["Open-Meteo", "Google API", "Fichier CSV Local"])
api_key = st.sidebar.text_input("Clé API Google", type="password") if "Google" in api_choice else ""

# ==========================================
# 5. CARTE DE SAISIE & ZONAGE INTELLIGENT
# ==========================================
st.title(tr("⚓ Port Terminal - SIG Géotechnique MASTER", "⚓ Port Terminal - PRO GIS MASTER"))

col_map, col_zones = st.columns([2, 1])

with col_map:
    map_style = st.radio("Vue :", ["Satellite", "Plan", "OSM"], horizontal=True)
    tiles = {'Satellite': 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', 'Plan': 'CartoDB Positron', 'OSM': 'OpenStreetMap'}
    m = folium.Map(location=st.session_state['map_center'], zoom_start=15, tiles=tiles[map_style], attr='Esri')
    
    # RECONSTRUCTION DES ZONES DESSINÉES DEPUIS LE JSON
    loaded_zones = []
    if st.session_state['project_data'] and 'zones' in st.session_state['project_data']:
        loaded_zones = st.session_state['project_data']['zones']
        for z in loaded_zones:
            folium.Polygon(locations=[(p[1], p[0]) for p in z['coords']], color='blue', fill=True).add_to(m)

    Draw(export=True).add_to(m)
    output = st_folium(m, width=800, height=500, key="input_map")

# EXTRACTION PRIORITAIRE : Si l'utilisateur a dessiné, on prend le dessin. Sinon, on prend le JSON.
drawn_polygons = []
if output and output.get("all_drawings"):
    drawn_polygons = [d["geometry"]["coordinates"][0] for d in output["all_drawings"]]
elif loaded_zones:
    drawn_polygons = [z['coords'] for z in loaded_zones]

zones_params = []
with col_zones:
    st.subheader("🗜️ Stratigraphie par Zone")
    if not drawn_polygons: st.info("Dessinez des zones sur la carte.")
    for i, poly in enumerate(drawn_polygons):
        z_load = loaded_zones[i] if i < len(loaded_zones) else {}
        
        with st.expander(f"🔴 Zone {i+1}", expanded=(i==0)):
            h_c = st.number_input("Épaisseur Argile (m)", value=float(z_load.get('H', 8.0)), key=f"h_{i}")
            c_ed, c_insitu = st.columns(2)
            with c_ed:
                cc = st.number_input("Cc", value=float(z_load.get('Cc', 0.45)), key=f"cc_{i}")
                cr = st.number_input("Cr", value=float(z_load.get('Cr', 0.05)), key=f"cr_{i}")
                e0 = st.number_input("e0", value=float(z_load.get('e0', 1.20)), key=f"e0_{i}")
                s0 = st.number_input("σ'0 [kPa]", value=float(z_load.get('sig_0', 40.0)), key=f"s0_{i}")
                sc = st.number_input("σ'c [kPa]", value=float(z_load.get('sig_c', 45.0)), key=f"sc_{i}")
            with c_insitu:
                su = st.number_input("Su [kPa]", value=float(z_load.get('Su', 18.0)), key=f"su_{i}")
                qt = st.number_input("qt [MPa]", value=float(z_load.get('qt', 0.60)), key=f"qt_{i}")
                alpha = st.number_input("α_M", value=float(z_load.get('alpha', 4.0)), key=f"a_{i}")
                n60 = st.number_input("N60", value=float(z_load.get('N60', 3.0)), key=f"n60_{i}")
                f2 = st.number_input("f2", value=float(z_load.get('f2', 500.0)), key=f"f2_{i}")
            ca = st.number_input("C_alpha", value=float(z_load.get('C_alpha', 0.015)), format="%.3f", key=f"ca_{i}")
            ch = st.number_input("c_h [m²/yr]", value=float(z_load.get('ch', 2.0)), key=f"ch_{i}")
            sp = st.number_input("Spacing PVD (m)", value=float(z_load.get('spacing', 1.2)), key=f"sp_{i}")
            
            zones_params.append({
                'id': i+1, 'coords': poly, 'H': h_c, 'Cc': cc, 'Cr': cr, 'e0': e0, 'sig_0': s0, 'sig_c': sc,
                'Su': su, 'qt': qt, 'alpha': alpha, 'N60': n60, 'f2': f2, 'C_alpha': ca, 'ch': ch, 'spacing': sp
            })

# ==========================================
# 6. COLLECTE API & CALCULS ITÉRATIFS
# ==========================================
st.markdown("---")
bt1, bt2, _ = st.columns([1, 1, 4])
with bt1: btn_api = st.button("🚀 1. COLLECTER MNT (API)", use_container_width=True, type="primary")
with bt2: btn_calc = st.button("🔄 2. CALCULER / MAJ ITÉRATIONS", use_container_width=True)

if btn_api:
    with st.spinner("Extraction MNT (Réseau API)..."):
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
            chunk_size = 50 
            
            if "Google" in api_choice and api_key:
                for i in range(0, len(all_pts), chunk_size):
                    chunk = all_pts[i:i+chunk_size]
                    locs = "|".join([f"{lt},{ln}" for lt, ln in chunk])
                    try:
                        r = requests.get(f"https://maps.googleapis.com/maps/api/elevation/json?locations={locs}&key={api_key}").json()
                        if r.get('status') == 'OK': elevs.extend([res['elevation'] for res in r['results']])
                        else: elevs.extend([2.0]*len(chunk))
                    except: elevs.extend([2.0]*len(chunk))
                    time.sleep(0.1)
            elif "Open-Meteo" in api_choice:
                for i in range(0, len(all_pts), chunk_size):
                    chunk = all_pts[i:i+chunk_size]
                    lats_str, lons_str = ",".join([str(round(p[0], 5)) for p in chunk]), ",".join([str(round(p[1], 5)) for p in chunk])
                    try:
                        resp = requests.get(f"https://api.open-meteo.com/v1/elevation?latitude={lats_str}&longitude={lons_str}")
                        if resp.status_code == 200: elevs.extend(resp.json().get('elevation', [2.0]*len(chunk)))
                        else: elevs.extend([2.0]*len(chunk))
                    except: elevs.extend([2.0]*len(chunk))
                    time.sleep(0.1)
            
            if elevs and len(elevs) == len(all_pts):
                st.session_state['cached_mnt'] = pd.DataFrame({'Lat': [p[0] for p in all_pts], 'Lon': [p[1] for p in all_pts], 'Z': elevs})
                st.success("Topographie API enregistrée !")
            else: st.error("Échec API.")

if btn_calc or (btn_api and st.session_state['cached_mnt'] is not None):
    if not zones_params or st.session_state['cached_mnt'] is None:
        st.error("Dessinez et collectez le MNT d'abord.")
    else:
        with st.spinner("Calculs géotechniques..."):
            results, all_pvds = [], pd.DataFrame()
            mnt = st.session_state['cached_mnt']
            for z in zones_params:
                poly_obj = Polygon(z['coords'])
                area = poly_obj.area * (111000**2) * math.cos(math.radians(poly_obj.centroid.y))
                mask = mnt.apply(lambda row: poly_obj.contains(Point(row['Lon'], row['Lat'])), axis=1)
                z_nat = mnt[mask]['Z'].mean() if not mnt[mask].empty else 2.0
                
                delta_z = z_target - z_nat
                q_remblai = max(0, delta_z * gamma_fill)
                q_surcharge = (q_remblai + dead_load + live_load) * surcharge_ratio
                
                s1 = calc_settlement_oedometer(z, q_surcharge)
                s2 = calc_settlement_cptu(z, q_surcharge)
                s3 = calc_settlement_spt(z, q_surcharge)
                s_max = max(s1, s2, s3)
                
                h_terre = max(0, delta_z) + s_max
                vol_tot = area * h_terre
                fs = (5.14 * z['Su']) / (gamma_fill * h_terre) if h_terre > 0 else 999
                
                df_p = generate_pvd_grid(z['coords'], z['spacing'])
                if not df_p.empty: df_p['Zone'] = z['id']; all_pvds = pd.concat([all_pvds, df_p])
                
                results.append({
                    'Zone': z['id'], 'Z_nat': z_nat, 'Delta_Z': delta_z, 'S_max': s_max, 'Vol': vol_tot, 
                    'FS': fs, 'Area': area, 'H_fill': h_terre, 'S_oedo': s1, 'S_cpt': s2, 'S_spt': s3
                })
            st.session_state['project_data'] = {'results': pd.DataFrame(results), 'pvds': all_pvds, 'mnt': mnt, 'zones': zones_params}
            st.success("Mise à jour des modèles terminée.")

# ==========================================
# 7. DASHBOARD MULTI-ONGLETS
# ==========================================
if st.session_state['project_data']:
    d = st.session_state['project_data']
    tabs = st.tabs(["🗺️ Topo & Contours", "⚠️ Tassements & Risques", "📍 Implantation PVD", "📉 Suivi & Coupes"])

    # --- TAB 1: TOPO ---

    with tabs[0]:
        # --- SÉCURITÉ JSON LÉGACY : Calcule la moyenne uniquement si Z_nat existe ---
        z_mean = d['results']['Z_nat'].mean() if 'Z_nat' in d['results'].columns else 0.0
        st.write(f"Altitude Moyenne du terrain naturel : **{z_mean:.2f} m**")
        
        c1, c2 = st.columns(2)
        # ... (la suite du code avec z_std = d['mnt']['Z'].std() etc. reste identique)
            z_std = d['mnt']['Z'].std()
            if z_std < 0.1: # Sécurité : Si écart < 10cm, terrain plat
                st.info("Terrain plat. Pas de variations suffisantes pour générer des courbes de niveau.")
            else:
                try:
                    triang = tri.Triangulation(d['mnt']['Lon'], d['mnt']['Lat'])
                    fig, ax = plt.subplots()
                    contour = ax.tricontourf(triang, d['mnt']['Z'], levels=15, cmap="terrain")
                    ax.tricontour(triang, d['mnt']['Z'], levels=15, colors='k', linewidths=0.3, alpha=0.5)
                    plt.colorbar(contour, ax=ax, label="Élévation (m)"); ax.set_xticks([]); ax.set_yticks([])
                    for spine in ax.spines.values(): spine.set_visible(False)
                    st.pyplot(fig); plt.close()
                except: st.warning("Graphique indisponible.")

        with c2:
            m_mnt = folium.Map(location=[d['mnt']['Lat'].mean(), d['mnt']['Lon'].mean()], zoom_start=16, tiles='CartoDB Positron')
            for z in d['zones']: folium.Polygon(locations=[(p[1], p[0]) for p in z['coords']], color='orange', weight=2, fill=False).add_to(m_mnt)
            lats_g = sorted(d['mnt']['Lat'].unique())[::4]; lons_g = sorted(d['mnt']['Lon'].unique())[::4]
            df_txt = d['mnt'][d['mnt']['Lat'].isin(lats_g) & d['mnt']['Lon'].isin(lons_g)]
            for _, r in df_txt.iterrows():
                folium.Marker([r['Lat'], r['Lon']], icon=folium.DivIcon(html=f'<div style="font-size:11px; color:darkred; font-weight:bold; transform:translate(-50%,-50%); text-shadow:1px 1px white,-1px -1px white;">{r["Z"]:.1f}</div>')).add_to(m_mnt)
            st_folium(m_mnt, width=600, height=400, key="mnt_inspect")

    # --- TAB 2: TASSEMENTS ---
    with tabs[1]:
        st.dataframe(d['results'].style.format({"Z_nat": "{:.2f}", "S_max": "{:.2f}", "Vol": "{:.0f}", "FS": "{:.2f}", "H_fill": "{:.2f}"}), use_container_width=True)
        for idx, row in d['results'].iterrows():
            with st.container():
                st.markdown(f"**Zone {int(row['Zone'])}**")
                s_oedo, s_cpt, s_spt = row.get('S_oedo', row['S_max']), row.get('S_cpt', row['S_max']), row.get('S_spt', row['S_max'])
                fs_val = row.get('FS', row.get('FS_Mudwave', 999.0))
                
                c_1, c_2 = st.columns(2)
                with c_1:
                    fig_c = go.Figure(data=[go.Bar(name='Oedo', x=['Modèles'], y=[s_oedo]), go.Bar(name='CPTu', x=['Modèles'], y=[s_cpt]), go.Bar(name='SPT', x=['Modèles'], y=[s_spt])])
                    fig_c.update_layout(height=250, margin=dict(t=30, b=0)); st.plotly_chart(fig_c, use_container_width=True)
                with c_2:
                    if fs_val < 1.3:
                        st.error(f"🚨 RISQUE MUDWAVE ! FS = {fs_val:.2f}")
                        st.info(f"💡 Conseil : Levée maximale de {(5.14*d['zones'][idx].get('Su', 15.0))/(gamma_fill*1.3):.2f}m.")
                    else: st.success(f"✅ FS Mudwave Stable ({fs_val:.2f})")

    # --- TAB 3: PVD ---
    with tabs[2]:
        tot_pvd = len(d['pvds'])
        st.metric("Total PVD", f"{tot_pvd:,.0f} unités")
        if not d['pvds'].empty:
            st.download_button("Export GPS PVD", d['pvds'].to_csv(index=False).encode('utf-8'), "layout_pvd.csv")
            fig_pvd = go.Figure()
            for z in d['zones']:
                x, y = Polygon(z['coords']).exterior.xy
                fig_pvd.add_trace(go.Scattermapbox(lat=list(y), lon=list(x), mode='lines', line=dict(width=3, color='red'), name=f"Zone {z['id']}"))
                df_zone = d['pvds'][d['pvds']['Zone'] == z['id']]
                if len(df_zone) > 2000:
                    df_vis = generate_pvd_grid(z['coords'], z.get('spacing', 1.2) * math.sqrt(len(df_zone)/2000.0))
                else: df_vis = df_zone
                if not df_vis.empty: fig_pvd.add_trace(go.Scattermapbox(lat=df_vis['Lat'], lon=df_vis['Lon'], mode='markers', marker=dict(size=3, color='blue', opacity=0.7), name=f"PVD Z{z['id']}"))
            fig_pvd.update_layout(mapbox_style="carto-positron", mapbox_center={"lat": d['mnt']['Lat'].mean(), "lon": d['mnt']['Lon'].mean()}, mapbox_zoom=15, height=500, margin=dict(r=0,t=0,l=0,b=0))
            st.plotly_chart(fig_pvd, use_container_width=True)

    # --- TAB 4: COUPES ---
    with tabs[3]:
        z_sel = st.selectbox("Sélection Zone", [f"Zone {z['id']}" for z in d['zones']])
        idx = int(z_sel.split(" ")[1]) - 1
        z_inf, r_inf = d['zones'][idx], d['results'].iloc[idx]
        h_fill_val, s_max_val, clay_h_val = r_inf.get('H_fill', r_inf.get('Fill_H', 0.0)), r_inf.get('S_max', 0.0), z_inf.get('H', 8.0)
        
        c_m1, c_m2 = st.columns([1, 2])
        with c_m1:
            st.write("**Coupe Géologique**")
            f_cp = go.Figure()
            f_cp.add_trace(go.Scatter(x=[0,100,100,0], y=[0,0,-clay_h_val,-clay_h_val], fill='toself', name='Argile', fillcolor='brown'))
            f_cp.add_trace(go.Scatter(x=[10,90,80,20], y=[0,0,h_fill_val,h_fill_val], fill='toself', name='Remblai', fillcolor='orange'))
            f_cp.update_layout(height=250, margin=dict(t=20, b=20)); st.plotly_chart(f_cp, use_container_width=True)
            df_mon = st.data_editor(pd.DataFrame({'Jour': [0, 30, 60, 90], 'Tassement_m': [0.0, 0.2, 0.45, 0.6]}), num_rows="dynamic")
        
        with c_m2:
            st.write("**Suivi de Consolidation**")
            days = np.linspace(0, 180 * 1.5, 100)
            S_th = [hansbo_consolidation(z_inf.get('ch', 2.0), z_inf.get('spacing', 1.2), t) * s_max_val for t in days]
            f_s = go.Figure()
            f_s.add_trace(go.Scatter(x=days, y=S_th, name='Design', line=dict(dash='dash', color='blue')))
            f_s.add_trace(go.Scatter(x=df_mon['Jour'], y=df_mon['Tassement_m'], name='Terrain', mode='markers+lines', marker=dict(color='red', size=10)))
            if len(df_mon) >= 3:
                s_ult, _, _, _, _ = calculate_asaoka(df_mon['Jour'].values, df_mon['Tassement_m'].values, 15)
                if s_ult:
                    f_s.add_hline(y=s_ult, line_color="orange", annotation_text=f"Asaoka ({s_ult:.2f}m)")
            f_s.add_hline(y=s_max_val, line_color="blue", annotation_text=f"S_ult Théorique ({s_max_val:.2f}m)")
            f_s.update_layout(height=450); st.plotly_chart(f_s, use_container_width=True)
