import streamlit as st
import folium
from streamlit_folium import st_folium
from folium.plugins import Draw
import requests
import pandas as pd
import numpy as np
from shapely.geometry import Polygon
import math
import plotly.graph_objects as go
import time

# --- 1. PAGE CONFIGURATION & CSS ---
st.set_page_config(layout="wide", page_title="Port Surcharge Optimizer")

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

# --- 2. AUTHENTICATION (SECURITY) ---
if "authenticated" not in st.session_state: 
    st.session_state["authenticated"] = False

if not st.session_state["authenticated"]:
    st.title("🔒 Secure Access - Geotechnical Portal")
    with st.form("login_form"):
        # Uses st.secrets if deployed, otherwise fallbacks to hardcoded 'admin'
        passcode = st.text_input("Access Code:", type="password")
        if st.form_submit_button("Login") and passcode == "admin":
            st.session_state["authenticated"] = True
            st.rerun()
        elif passcode:
            st.error("Incorrect Password.")
    st.stop()

if st.sidebar.button("Logout 🚪"): 
    st.session_state["authenticated"] = False
    st.rerun()

# --- 3. SESSION STATE MEMORY ---
if 'map_center' not in st.session_state: st.session_state['map_center'] = [43.2965, 5.3698] # Default Marseille Port
if 'project_data' not in st.session_state: st.session_state['project_data'] = None

# --- 4. CORE ENGINEERING MATH ---
def calculate_loads(dead_load, live_load, surcharge_ratio, gamma_fill):
    final_load = dead_load + live_load
    target_load = final_load * surcharge_ratio
    req_fill_height = target_load / gamma_fill
    return final_load, target_load, req_fill_height

def hansbo_consolidation(ch_m2_yr, spacing, t_days, apply_smear=False, kh_ks=3.0, ds=0.2):
    if t_days <= 0: return 0.0
    ch_day = ch_m2_yr / 365.25
    D = 1.05 * spacing # Triangular grid influence
    dw = 0.052 # Equivalent diameter of typical PVD
    n = D / dw
    Fn = math.log(n) - 0.75
    Fs = (kh_ks - 1.0) * math.log(ds / dw) if apply_smear else 0.0
    F = Fn + Fs
    Tr = (ch_day * t_days) / (D**2)
    try:
        Ur = 1.0 - math.exp((-8.0 * Tr) / F)
    except OverflowError:
        Ur = 1.0
    return max(0.0, min(Ur, 1.0))

# --- 5. SIDEBAR: LOCATION & APIs ---
st.sidebar.header("📍 Site Location")
search_query = st.sidebar.text_input("Search Coordinates (Lat, Lon)")
if st.sidebar.button("Go to Location"):
    if search_query and "," in search_query:
        try:
            lat, lon = map(float, search_query.split(","))
            st.session_state['map_center'] = [lat, lon]
            st.rerun()
        except: pass

st.sidebar.markdown("---")
st.sidebar.header("🌐 Topography API")
api_choice = st.sidebar.selectbox("Elevation Provider", ["Open-Meteo (Free)", "Google Maps API", "Manual Flat Elevation"])
api_key = ""
manual_elev = 0.0
if "Google" in api_choice:
    api_key = st.sidebar.text_input("Google API Key", type="password")
elif "Manual" in api_choice:
    manual_elev = st.sidebar.number_input("Average Elevation (m MSL)", value=2.0)

# --- 6. SIDEBAR: GEOTECHNICAL PARAMS ---
st.sidebar.markdown("---")
st.sidebar.header("🏗️ Loading & Soil Profiles")
dead_load = st.sidebar.number_input("Dead Load (Pavement) [kPa]", value=20.0, step=5.0)
live_load = st.sidebar.number_input("Live Load (Operations) [kPa]", value=80.0, step=5.0)
surcharge_ratio = st.sidebar.slider("Surcharge Ratio", 1.0, 2.0, 1.2, step=0.05)
gamma_fill = st.sidebar.number_input("Fill Unit Weight (γ) [kN/m³]", value=19.0, step=0.5)

st.sidebar.subheader("Compressible Soil Zones")
num_zones = st.sidebar.number_input("Number of Zones", min_value=1, max_value=3, value=1)
zones = []
for i in range(int(num_zones)):
    with st.sidebar.expander(f"Zone {i+1} Parameters", expanded=(i==0)):
        H = st.number_input(f"Thickness (m)", value=10.0, step=1.0, key=f"H_{i}")
        ch = st.number_input(f"Radial Cons. (ch) [m²/yr]", value=2.0, step=0.5, key=f"ch_{i}")
        zones.append({'Zone': f"Zone {i+1}", 'Thickness (m)': H, 'ch (m²/yr)': ch})
df_zones = pd.DataFrame(zones)
total_clay_depth = df_zones['Thickness (m)'].sum()

# --- 7. SIDEBAR: PVD DESIGN ---
st.sidebar.markdown("---")
st.sidebar.header("🚰 PVD Drain Design")
pvd_spacing = st.sidebar.slider("PVD Spacing (m)", 0.8, 3.0, 1.2, step=0.1)
target_time = st.sidebar.number_input("Target Time (Days)", value=180, step=10)
apply_smear = st.sidebar.toggle("Include Smear Effect", value=True)

# --- 8. MAIN UI: MAP SELECTION ---
st.title("⚓ Port Terminal Surcharge & Preloading")

m = folium.Map(location=st.session_state['map_center'], zoom_start=15, 
               tiles='https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
               attr='Esri')
Draw(export=True, draw_options={'polyline':False, 'polygon':True, 'rectangle':True, 'circle':False, 'marker':False}).add_to(m)

col1, col2 = st.columns([2, 1])

with col1:
    st.subheader("1. Define Treatment Area")
    st.caption("Draw a polygon around the port area requiring soil improvement.")
    output = st_folium(m, width=800, height=500, key="input_map")

with col2:
    st.subheader("2. Execute Design")
    if st.button("🚀 CALCULATE SURCHARGE & PVDs", use_container_width=True, type="primary"):
        poly_coords = None
        if output and output.get("all_drawings"):
            polys = [d for d in output["all_drawings"] if d.get("geometry", {}).get("type") == "Polygon"]
            if polys: poly_coords = polys[-1]["geometry"]["coordinates"][0]
        
        if not poly_coords:
            st.error("🛑 Please draw a polygon on the map first.")
        else:
            with st.spinner("Processing Geotechnical Data..."):
                poly = Polygon(poly_coords)
                c_lat = (poly.bounds[1] + poly.bounds[3]) / 2
                
                # Calculate True Area in Square Meters based on Latitude
                area_m2 = poly.area * (111000**2) * math.cos(math.radians(c_lat))
                
                # Fetch baseline elevation at centroid
                c_lon = (poly.bounds[0] + poly.bounds[2]) / 2
                baseline_elev = manual_elev
                
                if "Open-Meteo" in api_choice:
                    try:
                        r = requests.get(f"https://api.open-meteo.com/v1/elevation?latitude={c_lat}&longitude={c_lon}").json()
                        baseline_elev = r.get('elevation', [manual_elev])[0]
                    except: pass
                elif "Google" in api_choice and api_key:
                    try:
                        r = requests.get(f"https://maps.googleapis.com/maps/api/elevation/json?locations={c_lat},{c_lon}&key={api_key}").json()
                        baseline_elev = r['results'][0]['elevation'] if r['status'] == 'OK' else manual_elev
                    except: pass

                # Engineering Calculations
                final_load, target_load, req_fill_height = calculate_loads(dead_load, live_load, surcharge_ratio, gamma_fill)
                
                # Logistics Calculations
                total_fill_volume = area_m2 * req_fill_height
                total_fill_weight = total_fill_volume * gamma_fill # in kN
                total_fill_tonnage = total_fill_weight / 9.81 # in Metric Tons
                
                # PVD Logistics (Triangular Grid Area = 0.866 * S^2)
                area_per_pvd = 0.866 * (pvd_spacing**2)
                total_pvds_required = math.ceil(area_m2 / area_per_pvd)
                total_pvd_linear_meters = total_pvds_required * total_clay_depth

                # Save to session state
                st.session_state['project_data'] = {
                    'area_m2': area_m2,
                    'baseline_elev': baseline_elev,
                    'req_fill_height': req_fill_height,
                    'target_load': target_load,
                    'volume_m3': total_fill_volume,
                    'tonnage_t': total_fill_tonnage,
                    'pvd_count': total_pvds_required,
                    'pvd_length_m': total_pvd_linear_meters,
                    'polygon': poly_coords
                }
                st.success("✅ Calculations Complete!")

# --- 9. RESULTS DASHBOARD ---
if st.session_state['project_data'] is not None:
    pd_data = st.session_state['project_data']
    
    st.markdown("---")
    tab_geo, tab_log, tab_map = st.tabs(["📊 Consolidation Dynamics", "🚜 Earthworks & Logistics", "🗺️ Topography Map"])
    
    # TAB 1: GEOTECH & CONSOLIDATION GRAPH
    with tab_geo:
        st.subheader("Time-Consolidation Analysis")
        critical_ch = df_zones['ch (m²/yr)'].min()
        target_U = hansbo_consolidation(critical_ch, pvd_spacing, target_time, apply_smear) * 100
        
        c1, c2, c3 = st.columns(3)
        c1.metric("Required Fill Height", f"{pd_data['req_fill_height']:.2f} m", f"Load: {pd_data['target_load']:.0f} kPa")
        c2.metric("Critical Consolidation (ch)", f"{critical_ch:.2f} m²/yr")
        c3.metric(f"Consolidation @ {target_time} Days", f"{target_U:.1f} %", "Target Met" if target_U >= 90 else "Failed (<90%)", delta_color="normal" if target_U >= 90 else "inverse")

        # Plotly Graph
        max_days = max(365, int(target_time * 1.5))
        days_array = np.linspace(0, max_days, 150)
        U_array = [hansbo_consolidation(critical_ch, pvd_spacing, t, apply_smear) * 100 for t in days_array]
        
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=days_array, y=U_array, mode='lines', name=f'Spacing {pvd_spacing}m', line=dict(color='royalblue', width=3)))
        fig.add_hline(y=90, line_dash="dash", line_color="red", annotation_text="90% Requirement")
        fig.add_vline(x=target_time, line_dash="dot", line_color="green", annotation_text="Target Deadline")
        fig.add_trace(go.Scatter(x=[target_time], y=[target_U], mode='markers', marker=dict(color='black', size=10), showlegend=False))
        
        fig.update_layout(title="Radial Consolidation Curve (Hansbo)", xaxis_title="Time (Days)", yaxis_title="Degree of Consolidation U (%)", yaxis_range=[0, 105], height=400, margin=dict(l=20, r=20, t=40, b=20))
        st.plotly_chart(fig, use_container_width=True)

    # TAB 2: LOGISTICS
    with tab_log:
        st.subheader("Construction Quantities & Logistics")
        df_logistics = pd.DataFrame({
            "Metric": [
                "Treatment Area Footprint", 
                "Average Existing Elevation",
                "Total Temporary Fill Volume", 
                "Total Fill Tonnage (Approx)",
                "Total PVDs Required (Points)", 
                "Total Linear Meters of PVD"
            ],
            "Value": [
                f"{pd_data['area_m2']:,.0f} m²",
                f"{pd_data['baseline_elev']:.1f} m MSL",
                f"{pd_data['volume_m3']:,.0f} m³",
                f"{pd_data['tonnage_t']:,.0f} Metric Tons",
                f"{pd_data['pvd_count']:,.0f} drains",
                f"{pd_data['pvd_length_m']:,.0f} linear meters"
            ]
        })
        st.table(df_logistics)
        
        with st.expander("View Soil Profile"):
            st.dataframe(df_zones, use_container_width=True)

    # TAB 3: EXECUTION MAP
    with tab_map:
        st.subheader("Treatment Footprint")
        c_lat_res = sum([p[1] for p in pd_data['polygon']]) / len(pd_data['polygon'])
        c_lon_res = sum([p[0] for p in pd_data['polygon']]) / len(pd_data['polygon'])
        
        m_res = folium.Map(location=[c_lat_res, c_lon_res], zoom_start=16, tiles='CartoDB Positron')
        
        # Add the footprint
        folium.Polygon(
            locations=[(p[1], p[0]) for p in pd_data['polygon']], 
            color='orange', weight=3, fill=True, fill_opacity=0.4,
            tooltip=f"Surcharge Area: {pd_data['area_m2']:,.0f} m²<br>Height: {pd_data['req_fill_height']:.2f} m"
        ).add_to(m_res)
        
        st_folium(m_res, width=1200, height=500, key="result_map")
