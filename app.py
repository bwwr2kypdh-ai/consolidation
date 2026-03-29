import streamlit as st
import numpy as np
import matplotlib.pyplot as plt
import pandas as pd
import math

# ==========================================
# 1. CORE ENGINEERING FUNCTIONS
# ==========================================

def calculate_loads(dead_load, live_load, surcharge_ratio, gamma_fill):
    """Calculates final design load and required surcharge fill height."""
    final_load = dead_load + live_load
    target_load = final_load * surcharge_ratio
    req_fill_height = target_load / gamma_fill
    return final_load, target_load, req_fill_height

def hansbo_consolidation(ch_m2_yr, spacing, t_days, apply_smear=False, kh_ks=3.0, ds=0.2):
    """
    Calculates Degree of Consolidation (U) using Hansbo's Theory for Triangular Grid.
    Incorporates optional Smear Effect.
    """
    if t_days <= 0:
        return 0.0

    # Convert ch from m^2/year to m^2/day
    ch_day = ch_m2_yr / 365.25
    
    # Equivalent diameter of the influence zone (Triangular grid)
    D = 1.05 * spacing
    
    # Equivalent diameter of a standard PVD (e.g., 100mm x 4mm)
    a = 0.100  # width in meters
    b = 0.004  # thickness in meters
    dw = (a + b) / 2.0
    
    # n = spacing ratio
    n = D / dw
    
    # F(n): Spacing effect factor
    Fn = math.log(n) - 0.75
    
    # F(s): Smear effect factor
    Fs = 0.0
    if apply_smear:
        # ds = diameter of smear zone (typically 2-3 times mandrel equivalent diameter)
        # kh_ks = ratio of horizontal permeability in undisturbed zone vs smear zone
        Fs = (kh_ks - 1.0) * math.log(ds / dw)
        
    # Total resistance factor
    F = Fn + Fs
    
    # Time factor for radial drainage
    Tr = (ch_day * t_days) / (D**2)
    
    # Degree of consolidation (Ur)
    try:
        Ur = 1.0 - math.exp((-8.0 * Tr) / F)
    except OverflowError:
        Ur = 1.0 # If math.exp gets too large negative, Ur is essentially 100%
        
    return max(0.0, min(Ur, 1.0)) # Cap between 0% and 100%


# ==========================================
# 2. STREAMLIT WEB APP UI
# ==========================================

st.set_page_config(page_title="PVD & Surcharge Optimizer", layout="wide")
st.title("⚓ Port Terminal: PVD & Surcharge Optimizer")
st.markdown("Optimize preloading time by balancing surcharge fill height, PVD spacing, and complex soil behaviors.")

# --- SIDEBAR: SOIL ZONES & LOADING ---
st.sidebar.header("1. Loading Conditions")
dead_load = st.sidebar.number_input("Dead Load (Pavement) [kPa]", value=20.0, step=5.0)
live_load = st.sidebar.number_input("Live Load (Containers) [kPa]", value=80.0, step=5.0)
surcharge_ratio = st.sidebar.slider("Surcharge Ratio", min_value=1.0, max_value=2.0, value=1.20, step=0.05)
gamma_fill = st.sidebar.number_input("Fill Unit Weight (γ) [kN/m³]", value=19.0, step=0.5)

st.sidebar.markdown("---")
st.sidebar.header("2. Soil Profile (Zones)")
num_zones = st.sidebar.number_input("Number of Compressible Zones", min_value=1, max_value=5, value=2)

zones = []
for i in range(int(num_zones)):
    with st.sidebar.expander(f"Zone {i+1} Parameters", expanded=(i==0)):
        H = st.number_input(f"Thickness (m)", value=5.0, step=1.0, key=f"H_{i}")
        ch = st.number_input(f"Radial Cons. (ch) [m²/yr]", value=2.0, step=0.5, key=f"ch_{i}")
        zones.append({'Zone': f"Zone {i+1}", 'Thickness (m)': H, 'ch (m²/yr)': ch})

# Convert zones to a dataframe for display
df_zones = pd.DataFrame(zones)


# --- MAIN PANEL: PVD DESIGN & OPTIONS ---
col1, col2 = st.columns((1, 1))

with col1:
    st.header("3. PVD Design & Optimization")
    pvd_spacing = st.slider("PVD Spacing (m) [Triangular Grid]", min_value=0.8, max_value=3.0, value=1.2, step=0.1)
    target_time = st.number_input("Target Consolidation Time (Days)", value=180, step=10)

with col2:
    st.header("4. Advanced Parameters")
    apply_smear = st.toggle("Enable Smear Effect", value=True, help="Accounts for soil disturbance caused by driving the PVD mandrel, which reduces local permeability.")
    
    if apply_smear:
        kh_ks = st.slider("Permeability Ratio (kh/ks)", min_value=1.0, max_value=10.0, value=3.0, step=0.5, help="Ratio of horizontal permeability in undisturbed soil to the smear zone.")
        ds = st.number_input("Diameter of Smear Zone (m)", value=0.20, step=0.05, help="Typically 2 to 3 times the equivalent diameter of the mandrel.")
    else:
        kh_ks = 1.0
        ds = 0.0

# --- EXECUTING CALCULATIONS ---
final_load, target_load, req_fill_height = calculate_loads(dead_load, live_load, surcharge_ratio, gamma_fill)

# Find the critical (slowest) soil zone for conservative design
critical_ch = df_zones['ch (m²/yr)'].min()

# Calculate Target U%
target_U = hansbo_consolidation(critical_ch, pvd_spacing, target_time, apply_smear, kh_ks, ds)


# --- DISPLAY RESULTS ---
st.markdown("---")
st.header("📋 Design Output")

res_col1, res_col2, res_col3 = st.columns(3)
res_col1.metric("Required Fill Height", f"{req_fill_height:.2f} m", f"Target Load: {target_load:.0f} kPa")
res_col2.metric("Critical Consolidation (ch)", f"{critical_ch:.2f} m²/yr", "Slowest Zone")

# Formatting target metric based on success
target_U_pct = target_U * 100
if target_U_pct >= 90.0:
    res_col3.metric(f"Consolidation @ {target_time} Days", f"{target_U_pct:.1f} %", "Target Met (≥90%)", delta_color="normal")
else:
    res_col3.metric(f"Consolidation @ {target_time} Days", f"{target_U_pct:.1f} %", "Target Failed (<90%)", delta_color="inverse")

if target_U_pct >= 90.0:
    st.success("✅ The current PVD spacing and timeframe satisfy the 90% consolidation requirement for the most critical soil zone.")
else:
    st.error("⚠️ The required 90% consolidation is not met. Consider decreasing PVD spacing, increasing the time allowance, or increasing the surcharge ratio.")

# --- VISUALIZATION ---
st.markdown("---")
show_chart = st.toggle("Show Time-Consolidation Graph", value=True)

if show_chart:
    st.subheader("Consolidation Curve (Critical Soil Zone)")
    
    # Generate array of days for the X-axis (up to 1.5x target time or 365 days minimum)
    max_days = max(365, int(target_time * 1.5))
    days_array = np.linspace(0, max_days, 200)
    
    # Calculate U% for each day
    U_array = [hansbo_consolidation(critical_ch, pvd_spacing, t, apply_smear, kh_ks, ds) * 100 for t in days_array]
    
    # Plotting
    fig, ax = plt.subplots(figsize=(10, 4))
    
    # Main curve
    ax.plot(days_array, U_array, label=f"PVD Spacing: {pvd_spacing}m (ch={critical_ch})", color='#1f77b4', linewidth=2.5)
    
    # Reference lines
    ax.axhline(90, color='red', linestyle='--', linewidth=1.5, label="90% Design Requirement")
    ax.axvline(target_time, color='green', linestyle=':', linewidth=2, label=f"Target Deadline ({target_time} days)")
    
    # Intersection point
    ax.plot(target_time, target_U_pct, marker='o', markersize=8, color='black')
    ax.annotate(f"{target_U_pct:.1f}%", (target_time + 5, target_U_pct - 5), fontsize=10, weight='bold')

    # Styling
    ax.set_xlabel("Time (Days)", fontsize=11, fontweight='bold')
    ax.set_ylabel("Degree of Consolidation (%)", fontsize=11, fontweight='bold')
    ax.set_ylim(0, 105)
    ax.set_xlim(0, max_days)
    ax.grid(True, linestyle='--', alpha=0.6)
    ax.legend(loc="lower right")
    
    st.pyplot(fig)

    with st.expander("View Soil Profile Data"):
        st.dataframe(df_zones, use_container_width=True)
