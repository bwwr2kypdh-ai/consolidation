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
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ==========================================
# 1. PAGE CONFIGURATION & CSS
# ==========================================
st.set_page_config(layout="wide", page_title="Port Surcharge Optimizer MASTER")

st.markdown("""
<style>
@media print {
    .stSidebar {display: none !important;}
    header {display: none !important;}
    footer {display: none !important;}
    .stButton {display: none !important;}
}
</style>
""", unsafe_allow_html=True)

# ==========================================
# 2. TRANSLATION ENGINE & STATE
# ==========================================
if "lang" not in st.session_state: st.session_state["lang"] = "Français"

def tr(fr, en):
    return fr if st.session_state["lang"] == "Français" else en

if "authenticated" not in st.session_state: st.session_state["authenticated"] = False
if 'map_center' not in st.session_state: st.session_state['map_center'] = [43.2965, 5.3698]
if 'project_data' not in st.session_state: st.session_state['project_data'] = None
if 'raw_mnt' not in st.session_state: st.session_state['raw_mnt'] = None

# ==========================================
# 3. AUTHENTICATION
# ==========================================
def check_password():
    def password_entered():
        if st.session_state["pwd_input"].strip().lower() == "admin":
            st.session_state["authenticated"] = True
            del st.session_state["pwd_input"] 
        else:
            st.session_state["authenticated"] = False

    if not st.session_state.get("authenticated", False):
        st.title("🔒 Accès Sécurisé / Secure Access")
        st.text_input("Mot de passe / Password (admin) :", type="password", on_change=password_entered, key="pwd_input")
        if "authenticated" in st.session_state and not st.session_state["authenticated"]:
            st.error("Incorrect.")
        st.stop()

check_password()

# ==========================================
# 4. SIDEBAR : LANG & LOCATION
# ==========================================
st.session_state["lang"] = st.sidebar.radio("🌐 Language / Langue", ["Français", "English"])

if st.sidebar.button(tr("Se déconnecter 🚪", "Logout 🚪")):
    st.session_state["authenticated"] = False
    st.rerun()

st.sidebar.markdown("---")
st.sidebar.header(tr("📍 Localisation", "📍 Location"))
search_query = st.sidebar.text_input(tr("Adresse ou GPS", "Address or GPS"))
if st.sidebar.button(tr("Chercher", "Search")):
    if search_query:
        if "," in search_query and any(c.isdigit() for c in search_query):
            try:
                lat, lon = map(float, search_query.split(","))
                st.session_state['map_center'] = [lat, lon]
                st.rerun()
            except: pass
        else:
            try:
                url = f"https://nominatim.openstreetmap.org/search?q={search_query}&format=json&limit=1"
                res = requests.get(url, headers={'User-Agent': 'TopoApp/1.0'}).json()
                if res:
                    st.session_state['map_center'] = [float(res[0]['lat']), float(res[0]['lon'])]
                    st.rerun()
            except: st.sidebar.error(tr("Erreur réseau", "Network Error"))

# ==========================================
# 5. SIDEBAR : TOPOGRAPHY (MNT)
# ==========================================
st.sidebar.markdown("---")
st.sidebar.header(tr("🌐 Topographie (MNT)", "🌐 Topography (DEM)"))
api_choice = st.sidebar.selectbox(
    tr("Source d'élévation", "Elevation Source"), 
    ["Google Maps API", "Open-Meteo (Gratuit/Free)", "Fichier CSV Local / Local CSV File"]
)

api_key = ""
uploaded_mnt = None
if "Google" in api_choice:
    api_key = st.sidebar.text_input("Clé API / API Key", type="password")
elif "CSV" in api_choice:
    uploaded_mnt = st.sidebar.file_uploader(tr("Importer MNT (Lat, Lon, Z)", "Import DEM (Lat, Lon, Z)"), type=['csv'])

# ==========================================
# 6. SIDEBAR : GEOTECH PARAMS
# ==========================================
st.sidebar.markdown("---")
st.sidebar.header(tr("🏗️ Charges & Durée", "🏗️ Loads & Lifespan"))
dead_load = st.sidebar.number_input(tr("Charge Permanente [kPa]", "Dead Load [kPa]"), value=20.0, step=5.0)
live_load = st.sidebar.number_input(tr("Exploitation [kPa]", "Live Load [kPa]"), value=80.0, step=5.0)
surcharge_ratio = st.sidebar.slider("Surcharge Ratio", 1.0, 2.0, 1.2, step=0.05)
gamma_fill = st.sidebar.number_input(tr("Densité Remblai [kN/m³]", "Fill Unit Weight [kN/m³]"), value=19.0, step=0.5)
design_life = st.sidebar.number_input(tr("Durée de vie [Années]", "Design Life [Years]"), value=30, step=10)

st.sidebar.markdown("---")
st.sidebar.header(tr("🗜️ Stratigraphie", "🗜️ Stratigraphy"))
num_layers = st.sidebar.number_input(tr("Nombre de couches", "Number of layers"), min_value=1, max_value=5, value=1)

soil_layers = []
for i in range(int(num_layers)):
    with st.sidebar.expander(tr(f"Couche {i+1}", f"Layer {i+1}"), expanded=(i==0)):
        H = st.number_input(tr("Épaisseur (m)", "Thickness (m)"), value=8.0, key=f"H_{i}")
        e0 = st.number_input("e0", value=1.20, key=f"e0_{i}")
        Cc = st.number_input("Cc", value=0.45, key=f"Cc_{i}")
        Cr = st.number_input("Cr", value=0.05, key=f"Cr_{i}")
        sig_0 = st.number_input("σ'0 [kPa]", value=40.0, key=f"s0_{i}")
        sig_c = st.number_input("σ'c [kPa]", value=45.0, key=f"sc_{i}")
        C_alpha = st.number_input("C_alpha", value=0.015, format="%.3f", key=f"ca_{i}")
        Su = st.number_input("Su [kPa]", value=15.0, key=f"su_{i}")
        qt = st.number_input("qt [MPa]", value=0.60, key=f"qt_{i}")
        sig_v0 = st.number_input("σv0 [kPa]", value=50.0, key=f"sv0_{i}")
        alpha = st.number_input("α_M", value=4.0, key=f"a_{i}")
        N60 = st.number_input("N60", value=3.0, key=f"n60_{i}")
        f2 = st.number_input("f2 [kPa]", value=500.0, key=f"f2_{i}")
        
        soil_layers.append({'H': H, 'e0': e0, 'Cc': Cc, 'Cr': Cr, 'sig_0': sig_0, 'sig_c': sig_c, 'C_alpha': C_alpha, 'Su': Su, 'qt': qt, 'sig_v0': sig_v0, 'alpha': alpha, 'N60': N60, 'f2': f2})
total_H = sum(layer['H'] for layer in soil_layers)

st.sidebar.markdown("---")
st.sidebar.header(tr("🚰 Drainage (PVD)", "🚰 Drainage (PVD)"))
ch_val = st.sidebar.number_input("c_h [m²/yr]", value=2.0, step=0.5)
pvd_spacing = st.sidebar.slider(tr("Espacement PVD (m)", "PVD Spacing (m)"), 0.8, 3.0, 1.2, step=0.1)
target_time = st.sidebar.number_input(tr("Cible (Jours)", "Target (Days)"), value=180, step=10)

# ==========================================
# 7. ENGINEERING MATH FUNCTIONS
# ==========================================
def calc_settlement_oedometer_layered(layers, delta_sigma):
    total_S = 0.0
    for L in layers:
        final_stress = L['sig_0'] + delta_sigma
        if final_stress <= L['sig_c']: total_S += L['H'] * (L['Cr'] / (1 + L['e0'])) * math.log10(final_stress / L['sig_0'])
        else: total_S += (L['H'] * (L['Cr'] / (1 + L['e0'])) * math.log10(L['sig_c'] / L['sig_0'])) + (L['H'] * (L['Cc'] / (1 + L['e0'])) * math.log10(final_stress / L['sig_c']))
    return total_S

def calc_settlement_cptu_layered(layers, delta_sigma):
    return sum((delta_sigma / max(1000, L['alpha'] * ((L['qt'] * 1000) - L['sig_v0']))) * L['H'] for L in layers)

def calc_settlement_spt_layered(layers, delta_sigma):
    return sum((delta_sigma / max(1000, L['f2'] * L['N60'])) * L['H'] for L in layers)

def calc_secondary_compression(layers, t_days, t_years):
    t1 = t_days / 365.25; t2 = t1 + t_years
    return sum(L['C_alpha'] * L['H'] * math.log10(t2 / t1) for L in layers) if t1 > 0 else 0.0

def hansbo_consolidation(ch_m2_yr, spacing, t_days):
    if t_days <= 0: return 0.0
    Tr = (ch_m2_yr / 365.25 * t_days) / ((1.05 * spacing)**2)
    try: return max(0.0, min(1.0 - math.exp((-8.0 * Tr) / (math.log((1.05 * spacing)/0.052) - 0.75)), 1.0))
    except: return 1.0

# ==========================================
# 8. MAIN UI & MAP WITH MULTIPLE LAYERS
# ==========================================
st.title(tr("⚓ Port Terminal Optimizer MASTER", "⚓ Port Terminal Optimizer MASTER"))

# Initialisation de la carte sans "tiles" par défaut pour pouvoir en ajouter plusieurs
m = folium.Map(location=st.session_state['map_center'], zoom_start=16, tiles=None)

# Ajout des différentes couches (Vues)
folium.TileLayer(
    tiles='https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    attr='Esri',
    name=tr('Vue Satellite', 'Satellite View'),
    control=True
).add_to(m)

folium.TileLayer(
    tiles='OpenStreetMap',
    name=tr('Plan de rue (OSM)', 'Street Map (OSM)'),
    control=True
).add_to(m)

folium.TileLayer(
    tiles='CartoDB Positron',
    name=tr('Plan Clair', 'Light Map'),
    control=True
).add_to(m)

# Ajout du contrôleur de calques
folium.LayerControl(position='topright').add_to(m)

# Ajout des outils de dessin
Draw(export=True, draw_options={'polyline':False, 'polygon':True, 'rectangle':True, 'circle':False, 'marker':False}).add_to(m)

col1, col2 = st.columns([2, 1])
with col1:
    st.subheader(tr("1. Emprise du projet", "1. Project Footprint"))
    st.caption(tr("💡 Utilisez l'icône de calque en haut à droite de la carte pour changer de vue (Satellite/Plan).", "💡 Use the layer icon on the top right to switch views (Satellite/Map)."))
    output = st_folium(m, width=800, height=500, key="input_map")

with col2:
    st.subheader(tr("2. Traitement & Exécution", "2. Processing & Execution"))
    if st.button(tr("🚀 LANCER L'ANALYSE", "🚀 RUN ANALYSIS"), use_container_width=True, type="primary"):
        poly_coords = None
        if output and output.get("all_drawings"):
            polys = [d for d in output["all_drawings"] if d.get("geometry", {}).get("type") == "Polygon"]
            if polys: poly_coords = polys[-1]["geometry"]["coordinates"][0]
        
        if not poly_coords:
            st.error(tr("🛑 Veuillez dessiner un polygone.", "🛑 Please draw a polygon."))
        else:
            poly = Polygon(poly_coords)
            c_lat = (poly.bounds[1] + poly.bounds[3]) / 2
            area_m2 = poly.area * (111000**2) * math.cos(math.radians(c_lat))
            
            # --- MNT COLLECTION LOGIC ---
            with st.spinner(tr("Acquisition Topographique...", "Fetching Topography...")):
                df_mnt = None
                baseline_elev = 0.0
                
                if "CSV" in api_choice and uploaded_mnt is not None:
                    try:
                        df_mnt = pd.read_csv(uploaded_mnt)
                        z_col = [c for c in df_mnt.columns if 'Z' in c.upper() or 'ELEV' in c.upper()][0]
                        baseline_elev = df_mnt[z_col].mean()
                        st.session_state['raw_mnt'] = df_mnt
                    except: st.error(tr("Erreur de lecture CSV.", "CSV read error."))
                
                else: 
                    min_lon, min_lat, max_lon, max_lat = poly.bounds
                    res = 15.0 / 111000.0 
                    lons = np.arange(min_lon, max_lon, res)
                    lats = np.arange(min_lat, max_lat, res)
                    
                    valid_pts = []
                    for lat in lats:
                        for lon in lons:
                            if poly.contains(Point(lon, lat)): valid_pts.append((lat, lon))
                    
                    if not valid_pts:
                        valid_pts = [(c_lat, (poly.bounds[0] + poly.bounds[2])/2)] 
                        
                    elevs = []
                    if "Google" in api_choice and api_key:
                        for i in range(0, len(valid_pts), 50):
                            chunk = valid_pts[i:i+50]
                            locs = "|".join([f"{lat},{lon}" for lat, lon in chunk])
                            try:
                                r = requests.get(f"https://maps.googleapis.com/maps/api/elevation/json?locations={locs}&key={api_key}").json()
                                if r.get('status') == 'OK': elevs.extend([res['elevation'] for res in r['results']])
                                else: break
                            except: break
                            time.sleep(0.1)
                    elif "Open-Meteo" in api_choice:
                        lats_str = ",".join([str(p[0]) for p in valid_pts])
                        lons_str = ",".join([str(p[1]) for p in valid_pts])
                        try:
                            r = requests.get(f"https://api.open-meteo.com/v1/elevation?latitude={lats_str}&longitude={lons_str}").json()
                            if 'elevation' in r: elevs = r['elevation']
                        except: pass
                    
                    if elevs and len(elevs) == len(valid_pts):
                        df_mnt = pd.DataFrame({'Lat': [p[0] for p in valid_pts], 'Lon': [p[1] for p in valid_pts], 'Z': elevs})
                        baseline_elev = df_mnt['Z'].mean()
                        st.session_state['raw_mnt'] = df_mnt
            
            # --- ENGINEERING LOGIC ---
            with st.spinner(tr("Calculs Géotechniques...", "Geotechnical Calculations...")):
                final_load = dead_load + live_load
                target_load = final_load * surcharge_ratio
                base_fill_height = target_load / gamma_fill
                
                S_lab_u = calc_settlement_oedometer_layered(soil_layers, final_load)
                S_lab_t = calc_settlement_oedometer_layered(soil_layers, target_load)
                S_cpt = calc_settlement_cptu_layered(soil_layers, target_load)
                S_spt = calc_settlement_spt_layered(soil_layers, target_load)
                S_design = max(S_lab_t, S_cpt, S_spt)
                S_sec = calc_secondary_compression(soil_layers, target_time, design_life)
                
                actual_height = base_fill_height + S_design
                volume_m3 = area_m2 * actual_height
                
                min_Su = min(layer['Su'] for layer in soil_layers)
                q_ult = 5.14 * min_Su
                FS_mudwave = q_ult / (gamma_fill * actual_height) if actual_height > 0 else 999
                H_max_safe = q_ult / (gamma_fill * 1.3)
                
                area_per_pvd = 0.866 * (pvd_spacing**2)
                pvd_count = math.ceil(area_m2 / area_per_pvd)

                st.session_state['project_data'] = {
                    'area': area_m2, 'Z_avg': baseline_elev, 'load': target_load, 
                    'S_u': S_lab_u, 'S_t': S_lab_t, 'S_cpt': S_cpt, 'S_spt': S_spt, 'S_max': S_design, 'S_sec': S_sec,
                    'H_act': actual_height, 'Vol': volume_m3, 'PVD_cnt': pvd_count, 'PVD_ml': pvd_count * total_H,
                    'FS': FS_mudwave, 'H_safe': H_max_safe, 'Su': min_Su, 'poly': poly_coords
                }
                st.success(tr("✅ Analyse terminée.", "✅ Analysis complete."))

# ==========================================
# 9. RESULTS DASHBOARD
# ==========================================
if st.session_state['project_data'] is not None:
    d = st.session_state['project_data']
    
    st.markdown("---")
    t_topo, t_risk, t_geo, t_log, t_map = st.tabs([
        tr("🗺️ Topographie (MNT)", "🗺️ Topography (DEM)"),
        tr("⚠️ Risques & Tassements", "⚠️ Risks & Settlement"), 
        tr("📊 Consolidation", "📊 Consolidation"), 
        tr("🚜 Logistique", "🚜 Logistics"),
        tr("📍 Plan d'Exécution", "📍 Execution Map")
    ])
    
    # --- TAB 1: TOPOGRAPHY ---
    with t_topo:
        st.subheader(tr("Modèle Numérique de Terrain", "Digital Elevation Model"))
        c1, c2 = st.columns([1, 2])
        with c1:
            st.metric(tr("Altitude Moyenne", "Average Elevation"), f"{d['Z_avg']:.2f} m MSL")
            if st.session_state['raw_mnt'] is not None:
                csv = st.session_state['raw_mnt'].to_csv(index=False).encode('utf-8')
                st.download_button(label=tr("📥 Télécharger CSV", "📥 Download CSV"), data=csv, file_name='projet_mnt.csv', mime='text/csv')
                
    # --- TAB 2: RISKS ---
    with t_risk:
        c1, c2, c3 = st.columns(3)
        c1.metric(tr("1. Oedomètre", "1. Oedometer"), f"{d['S_t']:.3f} m")
        c2.metric("2. CPTu", f"{d['S_cpt']:.3f} m")
        c3.metric("3. SPT", f"{d['S_spt']:.3f} m")
        
        r1, r2 = st.columns(2)
        with r1:
            fig_comp = go.Figure(data=[
                go.Bar(name=tr('Primaire', 'Primary'), x=[tr('SANS', 'NO'), tr('AVEC', 'WITH')], y=[d['S_u'], d['S_t']], marker_color='royalblue'),
                go.Bar(name=tr('Fluage', 'Creep'), x=[tr('SANS', 'NO'), tr('AVEC', 'WITH')], y=[d['S_sec'], 0.0], marker_color='darkorange')
            ])
            fig_comp.update_layout(barmode='stack', yaxis_title="Tassement (m)", height=350); fig_comp.update_yaxes(autorange="reversed")
            st.plotly_chart(fig_comp, use_container_width=True)
            
        with r2:
            st.metric("Facteur Sécurité (FS)", f"{d['FS']:.2f}")
            if d['FS'] < 1.3: st.error(tr(f"Hauteur max par levée = {d['H_safe']:.2f} m.", f"Max lift height = {d['H_safe']:.2f} m."))
            else: st.success(tr("Portance OK.", "Bearing capacity OK."))

    # --- TAB 3: CONSOLIDATION ---
    with t_geo:
        target_U = hansbo_consolidation(ch_val, pvd_spacing, target_time) * 100
        days_array = np.linspace(0, max(365, int(target_time * 1.5)), 100)
        U_array = [hansbo_consolidation(ch_val, pvd_spacing, t) * 100 for t in days_array]
        fig_h = go.Figure(data=go.Scatter(x=days_array, y=U_array, mode='lines', line=dict(color='royalblue', width=3)))
        fig_h.add_vline(x=target_time, line_dash="dot", line_color="green", annotation_text=f"{target_U:.1f}%")
        st.plotly_chart(fig_h, use_container_width=True)

    # --- TAB 4: LOGISTICS ---
    with t_log:
        st.table(pd.DataFrame({
            tr("Indicateur", "Metric"): [tr("Surface", "Area"), tr("Volume Remblai", "Fill Volume"), tr("Quantité PVD", "PVD Quantity")],
            tr("Valeur", "Value"): [f"{d['area']:,.0f} m²", f"{d['Vol']:,.0f} m³", f"{d['PVD_cnt']:,.0f} drains"]
        }))

    # --- TAB 5: RESULT MAP ---
    with t_map:
        c_lat_res = sum([p[1] for p in d['poly']]) / len(d['poly'])
        c_lon_res = sum([p[0] for p in d['poly']]) / len(d['poly'])
        
        m_res = folium.Map(location=[c_lat_res, c_lon_res], zoom_start=16, tiles=None)
        
        # Ajout des calques pour la carte de résultats
        folium.TileLayer(tiles='CartoDB Positron', name=tr('Plan Clair', 'Light Map'), control=True).add_to(m_res)
        folium.TileLayer(tiles='https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', attr='Esri', name=tr('Vue Satellite', 'Satellite View'), control=True).add_to(m_res)
        folium.TileLayer(tiles='OpenStreetMap', name=tr('Plan de rue', 'Street Map'), control=True).add_to(m_res)
        
        folium.Polygon(locations=[(p[1], p[0]) for p in d['poly']], color='orange', weight=3, fill=True, fill_opacity=0.4).add_to(m_res)
        folium.LayerControl(position='topright').add_to(m_res)
        
        st_folium(m_res, width=1200, height=500, key="result_map")
