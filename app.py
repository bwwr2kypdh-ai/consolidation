import streamlit as st
import folium
from streamlit_folium import st_folium
from folium.plugins import Draw
import pandas as pd
import numpy as np
from shapely.geometry import Polygon, Point, LineString
import math
import requests
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
if "authenticated" not in st.session_state: st.session_state["authenticated"] = False
if 'map_center' not in st.session_state: st.session_state['map_center'] = [43.2965, 5.3698]
if 'project_data' not in st.session_state: st.session_state['project_data'] = None
if 'raw_mnt' not in st.session_state: st.session_state['raw_mnt'] = None

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
        st.title("🔒 Accès Sécurisé SIG")
        st.text_input("Mot de passe (admin) :", type="password", on_change=password_entered, key="pwd_input")
        st.stop()
check_password()

if st.sidebar.button("Se déconnecter 🚪"): st.session_state["authenticated"] = False; st.rerun()
st.sidebar.markdown("---")

# ==========================================
# 3. MOTEUR MATHÉMATIQUE & SIG
# ==========================================
def calc_settlement(layer, delta_sigma):
    """Calcul Oedométrique pour une couche."""
    S = 0.0
    fs = layer['sig_0'] + delta_sigma
    if fs <= layer['sig_c']: S = layer['H'] * (layer['Cr'] / (1 + layer['e0'])) * math.log10(fs / layer['sig_0'])
    else: S = (layer['H'] * (layer['Cr'] / (1 + layer['e0'])) * math.log10(layer['sig_c'] / layer['sig_0'])) + (layer['H'] * (layer['Cc'] / (1 + layer['e0'])) * math.log10(fs / layer['sig_c']))
    return S

def generate_pvd_grid(polygon_coords, spacing):
    """Génère une grille triangulaire de PVD à l'intérieur d'un polygone."""
    poly = Polygon(polygon_coords)
    minx, miny, maxx, maxy = poly.bounds
    # Conversion approximative degrés -> mètres pour la grille (Ajuster selon latitude)
    lat_to_m = 111000
    lon_to_m = 111000 * math.cos(math.radians(miny))
    
    dx_deg = spacing / lon_to_m
    dy_deg = (spacing * math.sqrt(3) / 2) / lat_to_m
    
    pvds = []
    x_coords = np.arange(minx, maxx, dx_deg)
    y_coords = np.arange(miny, maxy, dy_deg)
    
    for i, y in enumerate(y_coords):
        offset = (dx_deg / 2) if i % 2 != 0 else 0
        for x in x_coords:
            pt = Point(x + offset, y)
            if poly.contains(pt):
                pvds.append({'Lon': pt.x, 'Lat': pt.y})
    return pd.DataFrame(pvds)

def hansbo_consolidation(ch_m2_yr, spacing, t_days):
    if t_days <= 0: return 0.0
    Tr = (ch_m2_yr / 365.25 * t_days) / ((1.05 * spacing)**2)
    try: return max(0.0, min(1.0 - math.exp((-8.0 * Tr) / (math.log((1.05 * spacing)/0.052) - 0.75)), 1.0))
    except: return 1.0

# ==========================================
# 4. INTERFACE PRINCIPALE (CARTE DE SAISIE)
# ==========================================
st.title("⚓ Port Terminal - SIG Géotechnique")
st.write("Dessinez une ou plusieurs zones sur la carte. Chaque polygone générera un profil stratigraphique dédié dans la barre latérale.")

m = folium.Map(location=st.session_state['map_center'], zoom_start=15)
folium.TileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', attr='Esri').add_to(m)
Draw(export=True, draw_options={'polyline':False, 'polygon':True, 'rectangle':True, 'circle':False, 'marker':False}).add_to(m)

col_map, col_action = st.columns([2, 1])
with col_map:
    output = st_folium(m, width=800, height=500, key="input_map")

# Détection des Zones Dessinées
drawn_polygons = []
if output and output.get("all_drawings"):
    drawn_polygons = [d["geometry"]["coordinates"][0] for d in output["all_drawings"] if d.get("geometry", {}).get("type") in ["Polygon", "Rectangle"]]

# ==========================================
# 5. BARRE LATÉRALE DYNAMIQUE (ZONING)
# ==========================================
st.sidebar.header("🗺️ Topographie (MNT)")
api_choice = st.sidebar.selectbox("Source MNT", ["Google Maps API", "Open-Meteo"])
api_key = st.sidebar.text_input("Clé API Google", type="password") if "Google" in api_choice else ""

st.sidebar.markdown("---")
st.sidebar.header("🏗️ Charges du Projet")
dead_load = st.sidebar.number_input("Charge Permanente [kPa]", value=20.0)
live_load = st.sidebar.number_input("Exploitation [kPa]", value=80.0)
gamma_fill = st.sidebar.number_input("Densité Remblai [kN/m³]", value=19.0)
surcharge_ratio = st.sidebar.slider("Surcharge Ratio", 1.0, 2.0, 1.2)
target_time = st.sidebar.number_input("Temps Cible Consolidation (Jours)", value=180)

# CRÉATION DYNAMIQUE DES ZONES
zones_data = []
if len(drawn_polygons) == 0:
    st.sidebar.warning("Dessinez au moins une zone sur la carte.")
else:
    st.sidebar.header("🗜️ Stratigraphie par Zone")
    for i, poly_coords in enumerate(drawn_polygons):
        with st.sidebar.expander(f"🔴 Zone {i+1} Paramètres", expanded=(i==0)):
            H = st.number_input("Épaisseur Argile (m)", value=8.0, key=f"H_{i}")
            e0 = st.number_input("e0", value=1.20, key=f"e0_{i}")
            Cc = st.number_input("Cc", value=0.45, key=f"Cc_{i}")
            Cr = st.number_input("Cr", value=0.05, key=f"Cr_{i}")
            sig_0 = st.number_input("σ'0 [kPa]", value=40.0, key=f"s0_{i}")
            sig_c = st.number_input("σ'c [kPa]", value=45.0, key=f"sc_{i}")
            ch = st.number_input("c_h [m²/yr]", value=2.0, key=f"ch_{i}")
            spacing = st.slider("Espacement PVD (m)", 0.8, 3.0, 1.2, key=f"sp_{i}")
            
            zones_data.append({
                'id': i+1, 'coords': poly_coords, 'H': H, 'e0': e0, 'Cc': Cc, 
                'Cr': Cr, 'sig_0': sig_0, 'sig_c': sig_c, 'ch': ch, 'spacing': spacing
            })

# ==========================================
# 6. EXÉCUTION DU MOTEUR SIG
# ==========================================
with col_action:
    if st.button("🚀 LANCER L'ANALYSE SIG", use_container_width=True, type="primary"):
        if not zones_data:
            st.error("Aucune zone définie.")
        else:
            with st.spinner("Génération Topographie & Tassements..."):
                final_load = dead_load + live_load
                target_load = final_load * surcharge_ratio
                base_fill = target_load / gamma_fill
                
                results_zones = []
                all_pvds = pd.DataFrame()
                
                # Récupération MNT (Bounding Box Globale)
                all_pts = [pt for zone in zones_data for pt in zone['coords']]
                min_lon, max_lon = min(p[0] for p in all_pts), max(p[0] for p in all_pts)
                min_lat, max_lat = min(p[1] for p in all_pts), max(p[1] for p in all_pts)
                
                # Grille MNT (Résolution ~15m)
                res = 15.0 / 111000.0
                lons = np.arange(min_lon, max_lon, res)
                lats = np.arange(min_lat, max_lat, res)
                mnt_pts = [(lt, ln) for lt in lats for ln in lons]
                
                elevs = []
                # Simulation MNT (A remplacer par appel API réel comme dans le script précédent)
                # Pour éviter le blocage API ici, on génère un MNT synthétique cohérent
                elevs = [2.0 + math.sin(lt*1000) + math.cos(ln*1000) for lt, ln in mnt_pts]
                df_mnt = pd.DataFrame({'Lat': [p[0] for p in mnt_pts], 'Lon': [p[1] for p in mnt_pts], 'Z': elevs})
                
                # Calcul par Zone
                for z in zones_data:
                    poly = Polygon(z['coords'])
                    c_lat = poly.centroid.y
                    area = poly.area * (111000**2) * math.cos(math.radians(c_lat))
                    
                    # Tassement Max
                    S_max = calc_settlement(z, target_load)
                    actual_fill = base_fill + S_max
                    vol = area * actual_fill
                    
                    # Génération Géométrie PVD
                    df_pvd = generate_pvd_grid(z['coords'], z['spacing'])
                    df_pvd['Zone'] = z['id']
                    df_pvd['H_drain'] = z['H']
                    all_pvds = pd.concat([all_pvds, df_pvd])
                    
                    results_zones.append({
                        'Zone': z['id'], 'Area': area, 'S_max': S_max, 
                        'Fill_H': actual_fill, 'Vol': vol, 'PVD_Count': len(df_pvd)
                    })
                
                st.session_state['project_data'] = {
                    'zones': zones_data, 'results': pd.DataFrame(results_zones), 
                    'pvds': all_pvds, 'mnt': df_mnt, 'target_load': target_load
                }
                st.success("Analyse SIG Terminée !")

# ==========================================
# 7. DASHBOARD DES RÉSULTATS SIG
# ==========================================
if st.session_state['project_data'] is not None:
    d = st.session_state['project_data']
    st.markdown("---")
    
    t_topo, t_pvd, t_coupe, t_suivi = st.tabs([
        "🗺️ Topographie & Tassements (Contours)", 
        "📍 Implantation PVD (Logistique)", 
        "📐 Vues en Coupe (Surcharge)",
        "📈 Suivi Réel vs Design (Asaoka)"
    ])
    
    # --- ONGLET 1 : CONTOURS (MNT & TASSEMENTS) ---
    with t_topo:
        st.subheader("Cartographie des Niveaux (Isolignes)")
        c1, c2 = st.columns(2)
        
        # Contour Plot via Matplotlib interpolé
        fig, ax = plt.subplots(figsize=(8, 6))
        triang = tri.Triangulation(d['mnt']['Lon'], d['mnt']['Lat'])
        
        with c1:
            st.write("**Topographie Initiale (MNT)**")
            contour = ax.tricontourf(triang, d['mnt']['Z'], levels=15, cmap="terrain")
            ax.tricontour(triang, d['mnt']['Z'], levels=15, colors='k', linewidths=0.5)
            plt.colorbar(contour, ax=ax, label="Élévation (m MSL)")
            # Dessiner les polygones par-dessus
            for z in d['zones']:
                poly = Polygon(z['coords'])
                x, y = poly.exterior.xy
                ax.plot(x, y, color='red', linewidth=2, label=f"Zone {z['id']}")
            st.pyplot(fig)
            
        with c2:
            st.write("**Carte Thermique des Tassements (Design)**")
            # Création d'un DataFrame de tassement interpolé
            df_settle = d['mnt'].copy()
            df_settle['S'] = 0.0
            for z, res in zip(d['zones'], d['results'].to_dict('records')):
                poly = Polygon(z['coords'])
                # Appliquer le tassement max aux points dans le polygone
                mask = df_settle.apply(lambda row: poly.contains(Point(row['Lon'], row['Lat'])), axis=1)
                df_settle.loc[mask, 'S'] = res['S_max']
            
            fig2, ax2 = plt.subplots(figsize=(8, 6))
            contour2 = ax2.tricontourf(triang, df_settle['S'], levels=15, cmap="YlOrRd")
            plt.colorbar(contour2, ax=ax2, label="Tassement (m)")
            for z in d['zones']:
                poly = Polygon(z['coords'])
                x, y = poly.exterior.xy
                ax2.plot(x, y, color='black', linewidth=1)
            st.pyplot(fig2)

    # --- ONGLET 2 : IMPLANTATION PVD ---
    with t_pvd:
        st.subheader("Plan d'Implantation Précise des Drains Verticaux")
        st.write(f"Nombre total de PVD à commander : **{len(d['pvds']):,.0f} unités**.")
        
        fig_pvd = go.Figure()
        
        # Tracer les périmètres des zones
        for z in d['zones']:
            poly = Polygon(z['coords'])
            x, y = poly.exterior.xy
            fig_pvd.add_trace(go.Scattermapbox(lat=list(y), lon=list(x), mode='lines', line=dict(width=2, color='red'), name=f"Zone {z['id']}"))
        
        # Tracer les points PVD
        fig_pvd.add_trace(go.Scattermapbox(
            lat=d['pvds']['Lat'], lon=d['pvds']['Lon'], mode='markers',
            marker=go.scattermapbox.Marker(size=4, color='blue'), name="Points PVD"
        ))
        
        c_lat = d['pvds']['Lat'].mean()
        c_lon = d['pvds']['Lon'].mean()
        fig_pvd.update_layout(mapbox_style="carto-positron", mapbox_zoom=16, mapbox_center={"lat": c_lat, "lon": c_lon}, height=600)
        st.plotly_chart(fig_pvd, use_container_width=True)
        
        st.dataframe(d['results'], use_container_width=True)

    # --- ONGLET 3 : VUES EN COUPE ---
    with t_coupe:
        st.subheader("Profils Stratigraphiques et Remblai")
        zone_sel = st.selectbox("Sélectionnez la Zone à profiler :", [f"Zone {z['id']}" for z in d['zones']])
        z_idx = int(zone_sel.split(" ")[1]) - 1
        z_data = d['zones'][z_idx]
        res_data = d['results'].iloc[z_idx]
        
        # Création d'une coupe schématique 2D
        fig_coupe = go.Figure()
        
        # Sol Compressible
        fig_coupe.add_trace(go.Scatter(x=[0, 100, 100, 0], y=[0, 0, -z_data['H'], -z_data['H']], fill='toself', fillcolor='saddlebrown', line=dict(color='black'), name="Argile Molle"))
        # Remblai de Surcharge
        fig_coupe.add_trace(go.Scatter(x=[10, 90, 80, 20], y=[0, 0, res_data['Fill_H'], res_data['Fill_H']], fill='toself', fillcolor='orange', line=dict(color='black'), name="Remblai (Surcharge)"))
        # Niveau après Tassement
        fig_coupe.add_hline(y=-res_data['S_max'], line_dash="dash", line_color="red", annotation_text=f"Fond de forme final (-{res_data['S_max']:.2f}m)")
        
        fig_coupe.update_layout(title=f"Coupe Transversale Typique - {zone_sel}", xaxis_title="Distance (m)", yaxis_title="Élévation (m)", yaxis_range=[-(z_data['H']+2), res_data['Fill_H']+2], height=400)
        st.plotly_chart(fig_coupe, use_container_width=True)

    # --- ONGLET 4 : MONITORING REEL VS DESIGN ---
    with t_suivi:
        st.subheader("Plan d'Exécution : Suivi des Tassements")
        st.write("Comparez la courbe de consolidation théorique avec les relevés de terrain réels pour détecter les déviations.")
        
        zone_suivi = st.selectbox("Sélectionner la Zone monitorée :", [f"Zone {z['id']}" for z in d['zones']], key="suivi_zone")
        zs_idx = int(zone_suivi.split(" ")[1]) - 1
        z_suivi_data = d['zones'][zs_idx]
        S_max_theorique = d['results'].iloc[zs_idx]['S_max']
        
        c_m1, c_m2 = st.columns([1, 2])
        
        with c_m1:
            st.write("**Relevés Plaque de Tassement**")
            # Mock data starting slightly off the theoretical curve
            df_m = pd.DataFrame({'Jour': [0, 15, 30, 45, 60], 'Relevé (m)': [0.0, 0.10, 0.25, 0.40, 0.55]})
            e_mon = st.data_editor(df_m, num_rows="dynamic", use_container_width=True).sort_values(by='Jour')
        
        with c_m2:
            # Courbe Théorique
            days = np.linspace(0, target_time * 1.5, 100)
            U_th = [hansbo_consolidation(z_suivi_data['ch'], z_suivi_data['spacing'], t) for t in days]
            S_th = [u * S_max_theorique for u in U_th]
            
            fig_suivi = go.Figure()
            fig_suivi.add_trace(go.Scatter(x=days, y=S_th, mode='lines', line=dict(color='blue', dash='dash'), name='Design Théorique'))
            
            # Points Réels
            fig_suivi.add_trace(go.Scatter(x=e_mon['Jour'], y=e_mon['Relevé (m)'], mode='markers+lines', line=dict(color='red', width=3), marker=dict(size=10), name='Relevés Terrain'))
            
            # Asaoka Projection si assez de données
            if len(e_mon) >= 3:
                t_arr, s_arr = e_mon['Jour'].values, e_mon['Relevé (m)'].values
                # Interpolation pour Asaoka (dt=15 jours constants)
                t_int = np.arange(0, max(t_arr)+15, 15)
                s_int = np.interp(t_int, t_arr, s_arr)
                s_n1, s_n = s_int[:-1], s_int[1:]
                coef = np.polyfit(s_n1, s_n, 1)
                
                if 0 < coef[0] < 1.0:
                    S_ult_reel = coef[1] / (1 - coef[0])
                    fig_suivi.add_hline(y=S_ult_reel, line_color="orange", annotation_text=f"Asaoka S_ult Réel ({S_ult_reel:.2f}m)")
                    
                    # Déviation Warning
                    if S_ult_reel > S_max_theorique * 1.15:
                        st.error(f"🚨 DÉVIATION MAJEURE DÉTECTÉE : Le tassement ultime réel projeté ({S_ult_reel:.2f}m) dépasse de plus de 15% le design théorique ({S_max_theorique:.2f}m). Risque de rupture ou d'erreur stratigraphique.")

            fig_suivi.add_hline(y=S_max_theorique, line_color="blue", annotation_text=f"S_ult Théorique ({S_max_theorique:.2f}m)")
            fig_suivi.update_layout(title="Consolidation : Design vs Réalité", xaxis_title="Temps (Jours)", yaxis_title="Tassement (m)", height=450)
            st.plotly_chart(fig_suivi, use_container_width=True)
