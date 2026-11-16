"""
generate_all_figures.py
Generates all high-resolution PNG figures for the combined UGP report:
1. fig1_heat_decay.png - Heat equation modal decay & maximum principle
2. fig2_heat_convergence.png - Space O(h^2) and Time O(dt^4) convergence
3. fig3_heat_stability.png - Stability function R(z) & zig-zag blow-up
4. fig4_lorenz_attractor.png - 3D Lorenz Strange Attractor
5. fig5_lorenz_bifurcation.png - Route to chaos across rho
6. fig6_cavity_streamlines_vorticity.png - Lid-driven cavity streamlines & vorticity
7. fig7_cavity_re_profiles.png - Cavity centerline velocity profiles vs Ghia et al.
"""

import numpy as np
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
from scipy.sparse import diags, kron, eye
from scipy.sparse.linalg import splu

plt.rcParams.update({'font.size': 10, 'figure.autolayout': True})

print("1/7 Generating Fig 1: Heat equation decay & properties...")
# Heat Equation Experiment 1
N = 49; L = 1.0; alpha = 1.0; h = L / (N + 1)
x = np.linspace(h, L - h, N)
# Asymmetric Hat profile
a = 0.3
u0 = np.where(x <= a, x / a, (1.0 - x) / (1.0 - a))

# Exact Fourier coefficients
def fourier_exact(x, t, n_modes=200):
    u = np.zeros_like(x)
    for n in range(1, n_modes + 1):
        bn = (2.0 * np.sin(n * np.pi * a)) / ((n * np.pi)**2 * a * (1.0 - a))
        u += bn * np.exp(-alpha * (n * np.pi / L)**2 * t) * np.sin(n * np.pi * x / L)
    return u

# RK4 step for Heat
A_diag = -2.0 * np.ones(N); A_off = np.ones(N - 1)
def f_heat(u):
    du = np.zeros_like(u)
    du[0] = u[1] - 2*u[0]
    du[-1] = u[-2] - 2*u[-1]
    du[1:-1] = u[2:] - 2*u[1:-1] + u[:-2]
    return (alpha / h**2) * du

def rk4_heat(u, dt):
    k1 = f_heat(u)
    k2 = f_heat(u + 0.5*dt*k1)
    k3 = f_heat(u + 0.5*dt*k2)
    k4 = f_heat(u + dt*k3)
    return u + (dt / 6.0) * (k1 + 2*k2 + 2*k3 + k4)

dt = 1e-4; times = [0.0, 0.002, 0.005, 0.01, 0.03, 0.1]
steps = int(0.1 / dt)
u_curr = u0.copy()
profiles = {0.0: u_curr.copy()}
for s in range(1, steps + 1):
    u_curr = rk4_heat(u_curr, dt)
    curr_t = round(s * dt, 4)
    if curr_t in times:
        profiles[curr_t] = u_curr.copy()

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 4))
for t in times:
    ax1.plot(x, profiles[t], 'o', markersize=3, label=f'RK4 t={t}')
    ax1.plot(x, fourier_exact(x, t), '-', color='black', alpha=0.5)
ax1.set_title("A. Temperature Profile Evolution")
ax1.set_xlabel("x"); ax1.set_ylabel("u(x,t)"); ax1.grid(True)

# Energy and Max Principle
max_u = []; energy = []; time_arr = np.linspace(0, 0.1, 100)
u_t = u0.copy()
for _ in range(100):
    max_u.append(np.max(u_t))
    energy.append(np.trapezoid(u_t**2, x))
    for _ in range(int(0.001/dt)):
        u_t = rk4_heat(u_t, dt)
ax2.plot(time_arr, max_u / max_u[0], label="Max |u(t)| / Max |u(0)|", lw=1.8)
ax2.plot(time_arr, energy / energy[0], '--', label="Energy E(t) / E(0)", lw=1.8)
ax2.set_title("B. Dissipation & Maximum Principle")
ax2.set_xlabel("t"); ax2.set_ylabel("Normalized Value"); ax2.legend(); ax2.grid(True)
plt.savefig("fig1_heat_decay.png", dpi=300)
plt.close()

print("2/7 Generating Fig 2: Heat equation convergence...")
T_end = 0.1

def heat_rhs(v, hh):
    d = np.empty_like(v)
    d[0] = v[1] - 2*v[0]; d[-1] = v[-2] - 2*v[-1]
    d[1:-1] = v[2:] - 2*v[1:-1] + v[:-2]
    return (alpha / hh**2) * d

def heat_march(u, hh, n_steps, scheme, T=T_end):
    dtt = T / n_steps
    for _ in range(n_steps):
        if scheme == 'rk4':
            k1 = heat_rhs(u, hh)
            k2 = heat_rhs(u + 0.5*dtt*k1, hh)
            k3 = heat_rhs(u + 0.5*dtt*k2, hh)
            k4 = heat_rhs(u + dtt*k3, hh)
            u = u + (dtt / 6.0) * (k1 + 2*k2 + 2*k3 + k4)
        else:
            u = u + dtt * heat_rhs(u, hh)
    return u

def loglog_slope(xs, ys):
    return np.polyfit(np.log(xs), np.log(ys), 1)[0]

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 4))
# Spatial convergence: RK4 with a tiny dt (time error negligible) vs exact Fourier solution at T=0.1
h_vals = []; err_space = []
for Nn in [19, 39, 79, 159]:     # h = 0.05, 0.025, 0.0125, 0.00625 -> the kink at x=a=0.3 is always a grid node
    hh = L / (Nn + 1); xx = np.linspace(hh, L - hh, Nn)
    u_start = np.where(xx <= a, xx / a, (1.0 - xx) / (1.0 - a))
    n_steps = int(np.ceil(T_end / (0.2 * hh**2)))
    u_num = heat_march(u_start, hh, n_steps, 'rk4')
    h_vals.append(hh); err_space.append(np.max(np.abs(u_num - fourier_exact(xx, T_end))))
h_vals = np.array(h_vals); err_space = np.array(err_space)
p_space = loglog_slope(h_vals, err_space)
ax1.loglog(h_vals, err_space, 'o-', label=f"Measured error at T=0.1 (slope {p_space:.2f})", lw=1.8)
ax1.loglog(h_vals, err_space[0] * (h_vals / h_vals[0])**2, '--', label=r"$\mathcal{O}(h^2)$ slope guide")
ax1.set_title(r"A. Spatial Convergence $\mathcal{O}(h^2)$")
ax1.set_xlabel("Grid step h"); ax1.set_ylabel("Max Error"); ax1.legend(); ax1.grid(True)
print(f"   spatial errors {err_space}, slope {p_space:.2f}")

# Temporal convergence: error vs the exact semi-discrete solution (matrix exponential via eigen-decomposition)
A_mat = (alpha / h**2) * (np.diag(-2.0*np.ones(N)) + np.diag(np.ones(N-1), 1) + np.diag(np.ones(N-1), -1))
lam, V = np.linalg.eigh(A_mat)
T_t = 0.002                                 # early time: the kink's high modes are still alive, so errors sit above round-off
u_ref = V @ (np.exp(lam * T_t) * (V.T @ u0))
n_list = 16 * 2**np.arange(6)               # dt = 1.25e-4 ... 3.9e-6 (stable for both: Euler needs dt <= 2e-4)
n_euler = n_list
dt_rk4 = T_t / n_list; dt_euler = T_t / n_euler
err_rk4 = np.array([np.max(np.abs(heat_march(u0, h, n, 'rk4', T_t) - u_ref)) for n in n_list])
err_euler = np.array([np.max(np.abs(heat_march(u0, h, n, 'euler', T_t) - u_ref)) for n in n_euler])
# slope of RK4 fitted only on points above the round-off floor
ok = err_rk4 > 1e-13
p_rk4 = loglog_slope(dt_rk4[ok], err_rk4[ok]); p_euler = loglog_slope(dt_euler, err_euler)
ax2.loglog(dt_euler, err_euler, 's-', color='tab:red', label=f"Euler (slope {p_euler:.2f})", lw=1.8)
ax2.loglog(dt_rk4, err_rk4, 'o-', color='tab:blue', label=f"RK4 (slope {p_rk4:.2f})", lw=1.8)
ax2.set_title(r"B. Temporal Convergence $\mathcal{O}(\Delta t^4)$ vs $\mathcal{O}(\Delta t)$")
ax2.set_xlabel(r"Time step $\Delta t$ (T=0.002)"); ax2.set_ylabel("Max Error vs Semi-discrete"); ax2.legend(); ax2.grid(True)
print(f"   euler errors {err_euler}, slope {p_euler:.2f}")
print(f"   rk4 errors {err_rk4}, slope {p_rk4:.2f}")
plt.savefig("fig2_heat_convergence.png", dpi=300)
plt.close()

print("3/7 Generating Fig 3: Heat equation stability...")
z = np.linspace(-4.0, 0.5, 400)
R_euler = np.abs(1.0 + z)
R_rk4 = np.abs(1.0 + z + z**2/2.0 + z**3/6.0 + z**4/24.0)

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 4))
ax1.plot(z, R_euler, label="Euler $|1+z|$", color='tab:red', lw=1.8)
ax1.plot(z, R_rk4, label=r"RK4 $|R_{RK4}(z)|$", color='tab:blue', lw=1.8)
ax1.axhline(1.0, color='black', linestyle=':', label="|R|=1 (Stability boundary)")
ax1.axvline(-2.0, color='tab:red', linestyle='--', alpha=0.7)
ax1.axvline(-2.7853, color='tab:blue', linestyle='--', alpha=0.7)
ax1.set_ylim(0, 3.0); ax1.set_title("A. Linear Stability Function |R(z)|")
ax1.set_xlabel(r"$z = \lambda \Delta t$"); ax1.set_ylabel("|R(z)|"); ax1.legend(); ax1.grid(True)

# Blow up demonstration: march the actual highest-frequency (zig-zag) eigenmode of the N=49 operator with RK4
steps_sim = 250
t_sim = np.arange(steps_sim + 1)
mode = np.sin(N * np.pi * x / L); mode /= np.max(np.abs(mode))
lam_max = -4.0 * alpha / h**2 * np.sin(N * np.pi / (2.0 * (N + 1)))**2
z_lim = -2.7853
def zigzag_amplitude(frac):
    dt_z = frac * z_lim / lam_max               # dt so that z = lam_max*dt = frac * (RK4 stability limit)
    v = 1e-15 * mode
    amp = [np.max(np.abs(v))]
    for _ in range(steps_sim):
        v = rk4_heat(v, dt_z)
        amp.append(np.max(np.abs(v)))
    zz = lam_max * dt_z
    return np.array(amp), abs(1 + zz + zz**2/2 + zz**3/6 + zz**4/24)
amp_stable, R_st = zigzag_amplitude(0.95)
amp_unstable, R_un = zigzag_amplitude(1.05)
ax2.semilogy(t_sim, amp_stable, label=f"Stable ($z=0.95\\,z_{{lim}}$, $|R|={R_st:.3f}$)", color='tab:green', lw=1.8)
ax2.semilogy(t_sim, amp_unstable, label=f"Unstable ($z=1.05\\,z_{{lim}}$, $|R|={R_un:.3f}$)", color='tab:red', lw=1.8)
ax2.axhline(1.0, color='black', linestyle=':', label="Macroscopic Error (1.0)")
ax2.set_ylim(1e-30, 1e8)
ax2.set_title("B. High-Frequency Zig-Zag Mode Evolution")
ax2.set_xlabel("Time Steps"); ax2.set_ylabel("Mode Amplitude"); ax2.legend(loc='upper left', fontsize=8); ax2.grid(True)
plt.savefig("fig3_heat_stability.png", dpi=300)
plt.close()

print("4/7 Generating Fig 4: Lorenz strange attractor...")
def lorenz(state, sigma=10.0, rho=28.0, beta=8.0/3.0):
    x, y, z = state
    return np.array([sigma*(y - x), x*(rho - z) - y, x*y - beta*z])

dt_l = 0.01; steps_l = 4000
u_l = np.array([1.0, 1.0, 1.0])
traj = np.zeros((steps_l, 3))
for i in range(steps_l):
    traj[i] = u_l
    k1 = lorenz(u_l)
    k2 = lorenz(u_l + 0.5*dt_l*k1)
    k3 = lorenz(u_l + 0.5*dt_l*k2)
    k4 = lorenz(u_l + dt_l*k3)
    u_l += (dt_l / 6.0) * (k1 + 2*k2 + 2*k3 + k4)

fig = plt.figure(figsize=(10, 4.5))
ax1 = fig.add_subplot(121, projection='3d')
ax1.plot(traj[500:, 0], traj[500:, 1], traj[500:, 2], lw=0.6, color='navy')
ax1.set_title(r"A. 3D Phase Space ($\rho=28$)")
ax1.set_xlabel("x"); ax1.set_ylabel("y"); ax1.set_zlabel("z")

ax2 = fig.add_subplot(122)
ax2.plot(traj[500:, 0], traj[500:, 2], lw=0.6, color='darkgreen')
ax2.set_title(r"B. Projection on x-z Plane")
ax2.set_xlabel("x"); ax2.set_ylabel("z"); ax2.grid(True)
plt.savefig("fig4_lorenz_attractor.png", dpi=300)
plt.close()

print("5/7 Generating Fig 5: Lorenz route to chaos...")
# Bifurcation sequence
fig, axes = plt.subplots(1, 3, figsize=(12, 3.5))
rhos = [10.0, 24.5, 100.0]
titles = [r"$\rho=10$ (Steady Spiral $C_+$)", r"$\rho=24.5$ (Coexistence Chaos)", r"$\rho=100$ (Periodic Orbit)"]
n_steps_rho = {10.0: 3000, 24.5: 3000, 100.0: 20000}     # rho=100 needs a long run for the transient to die out
skip_rho = {10.0: 300, 24.5: 300, 100.0: 12000}
for ax, r, tit in zip(axes, rhos, titles):
    u_tmp = np.array([1.0, 1.0, 1.0])
    tr = np.zeros((n_steps_rho[r], 3))
    for i in range(n_steps_rho[r]):
        tr[i] = u_tmp
        k1 = lorenz(u_tmp, rho=r)
        k2 = lorenz(u_tmp + 0.5*dt_l*k1, rho=r)
        k3 = lorenz(u_tmp + 0.5*dt_l*k2, rho=r)
        k4 = lorenz(u_tmp + dt_l*k3, rho=r)
        u_tmp += (dt_l/6.0)*(k1 + 2*k2 + 2*k3 + k4)
    ax.plot(tr[skip_rho[r]:, 0], tr[skip_rho[r]:, 2], lw=0.6, color='purple')
    ax.set_title(tit)
    ax.set_xlabel("x"); ax.set_ylabel("z"); ax.grid(True)
plt.savefig("fig5_lorenz_bifurcation.png", dpi=300)
plt.close()

print("6/7 Generating Fig 6: Lid-driven cavity simulation...")
N_c = 41; L_c = 1.0; h_c = L_c / (N_c - 1)
xc = np.linspace(0, L_c, N_c); yc = np.linspace(0, L_c, N_c)
Xc, Yc = np.meshgrid(xc, yc)
Re_c = 100.0; dt_c = 0.005; max_steps_c = 20000; tol_c = 1e-7   # march until steady state (t ~ 20-30 at Re=100)

M_c = N_c - 2
main_diag = -2.0 * np.ones(M_c); off_diag = np.ones(M_c - 1)   # 1-D second difference; kron(I,D)+kron(D,I) gives the 5-point stencil (-4 centre); off_diag = np.ones(M_c - 1)
D1D = diags([off_diag, main_diag, off_diag], [-1, 0, 1], shape=(M_c, M_c))
I_1D = eye(M_c)
A_poisson = (kron(I_1D, D1D) + kron(D1D, I_1D)) / (h_c**2)
A_lu = splu(A_poisson.tocsc())   # factorize once, reuse every step

def solve_psi(w):
    rhs = -w[1:-1, 1:-1].flatten()
    psi_in = A_lu.solve(rhs)
    p = np.zeros((N_c, N_c))
    p[1:-1, 1:-1] = psi_in.reshape((M_c, M_c))
    return p

def wall_w(p):
    wb = np.zeros((N_c, N_c))
    wb[0, :] = -2.0 * p[1, :] / (h_c**2)
    wb[-1, :] = -2.0 * p[-2, :] / (h_c**2) - 2.0 / h_c
    wb[:, 0] = -2.0 * p[:, 1] / (h_c**2)
    wb[:, -1] = -2.0 * p[:, -2] / (h_c**2)
    return wb

def rhs_w(w_in, w_full, p):
    u = (p[2:, 1:-1] - p[:-2, 1:-1]) / (2.0 * h_c)
    v = -(p[1:-1, 2:] - p[1:-1, :-2]) / (2.0 * h_c)
    dw_dx = (w_full[1:-1, 2:] - w_full[1:-1, :-2]) / (2.0 * h_c)
    dw_dy = (w_full[2:, 1:-1] - w_full[:-2, 1:-1]) / (2.0 * h_c)
    lap_w = (w_full[1:-1, 2:] + w_full[1:-1, :-2] + w_full[2:, 1:-1] + w_full[:-2, 1:-1] - 4.0 * w_full[1:-1, 1:-1]) / (h_c**2)
    return -(u * dw_dx + v * dw_dy) + (1.0 / Re_c) * lap_w

omega_c = np.zeros((N_c, N_c))
psi_c = np.zeros((N_c, N_c))

for step_c in range(1, max_steps_c + 1):
    wb = wall_w(psi_c)
    omega_c[0, :] = wb[0, :]; omega_c[-1, :] = wb[-1, :]
    omega_c[:, 0] = wb[:, 0]; omega_c[:, -1] = wb[:, -1]
    y_n = omega_c[1:-1, 1:-1].copy()
    
    k1 = rhs_w(y_n, omega_c, psi_c)
    y2 = y_n + 0.5*dt_c*k1; w2 = omega_c.copy(); w2[1:-1, 1:-1] = y2
    k2 = rhs_w(y2, w2, psi_c)
    y3 = y_n + 0.5*dt_c*k2; w3 = omega_c.copy(); w3[1:-1, 1:-1] = y3
    k3 = rhs_w(y3, w3, psi_c)
    y4 = y_n + dt_c*k3; w4 = omega_c.copy(); w4[1:-1, 1:-1] = y4
    k4 = rhs_w(y4, w4, psi_c)
    
    omega_c[1:-1, 1:-1] += (dt_c / 6.0) * (k1 + 2*k2 + 2*k3 + k4)
    psi_c = solve_psi(omega_c)
    change_c = np.max(np.abs(omega_c[1:-1, 1:-1] - y_n)) / dt_c
    if change_c < tol_c:
        break
print(f"   cavity: {step_c} steps (t = {step_c*dt_c:.1f}), max |d(omega)/dt| = {change_c:.2e}")
wb = wall_w(psi_c)
omega_c[0, :] = wb[0, :]; omega_c[-1, :] = wb[-1, :]
omega_c[:, 0] = wb[:, 0]; omega_c[:, -1] = wb[:, -1]

u_vel = np.zeros((N_c, N_c)); v_vel = np.zeros((N_c, N_c))
u_vel[1:-1, 1:-1] = (psi_c[2:, 1:-1] - psi_c[:-2, 1:-1]) / (2.0 * h_c)
v_vel[1:-1, 1:-1] = -(psi_c[1:-1, 2:] - psi_c[1:-1, :-2]) / (2.0 * h_c)
u_vel[-1, :] = 1.0

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.5))
ax1.streamplot(xc, yc, u_vel, v_vel, color='navy', density=1.3, linewidth=1.1)
ax1.set_title(r"A. Streamlines $\psi$ (Primary Circulation Vortex)")
ax1.set_xlabel("x"); ax1.set_ylabel("y"); ax1.set_aspect('equal')

c = ax2.contourf(Xc, Yc, omega_c, levels=30, cmap='RdBu_r')
fig.colorbar(c, ax=ax2, label=r"Vorticity $\omega$")
ax2.set_title(r"B. Vorticity Field $\omega$")
ax2.set_xlabel("x"); ax2.set_ylabel("y"); ax2.set_aspect('equal')
plt.savefig("fig6_cavity_streamlines_vorticity.png", dpi=300)
plt.close()

print("7/7 Generating Fig 7: Cavity centerline velocity profiles...")
# Benchmark data from Ghia, Ghia & Shin (1982) for Re=100
ghia_y = np.array([0.0, 0.0547, 0.0625, 0.0703, 0.1016, 0.1719, 0.2813, 0.4531, 0.5, 0.6172, 0.7344, 0.8516, 0.9531, 0.9609, 0.9688, 0.9766, 1.0])
ghia_u = np.array([0.0, -0.03717, -0.04192, -0.04775, -0.06434, -0.10150, -0.15662, -0.21090, -0.20581, -0.13641, 0.00332, 0.23151, 0.68717, 0.73722, 0.78871, 0.84123, 1.0])
ghia_x = np.array([0.0, 0.0625, 0.0703, 0.0781, 0.0938, 0.1563, 0.2266, 0.2344, 0.5, 0.8047, 0.8594, 0.9063, 0.9453, 0.9531, 0.9609, 0.9688, 1.0])
ghia_v = np.array([0.0, 0.09233, 0.10091, 0.10890, 0.12317, 0.16077, 0.17507, 0.17527, 0.05454, -0.24533, -0.22445, -0.16914, -0.10313, -0.08864, -0.07391, -0.05906, 0.0])

mid_x = N_c // 2
mid_y = N_c // 2

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 4))
ax1.plot(u_vel[:, mid_x], yc, 'b-', label="RK4 Numerical (Re=100)", lw=1.8)
ax1.plot(ghia_u, ghia_y, 'ro', label="Ghia et al. (1982) Benchmark", markersize=5)
ax1.set_title("A. Horizontal Velocity u(y) along Vertical Centerline")
ax1.set_xlabel("u(0.5, y)"); ax1.set_ylabel("y"); ax1.legend(); ax1.grid(True)

ax2.plot(xc, v_vel[mid_y, :], 'g-', label="RK4 Numerical (Re=100)", lw=1.8)
ax2.plot(ghia_x, ghia_v, 'ro', label="Ghia et al. (1982) Benchmark", markersize=5)
print(f"   max |u - Ghia_u| = {np.max(np.abs(np.interp(ghia_y, yc, u_vel[:, mid_x]) - ghia_u)):.4f}, "
      f"max |v - Ghia_v| = {np.max(np.abs(np.interp(ghia_x, xc, v_vel[mid_y, :]) - ghia_v)):.4f}")
ax2.set_title("B. Vertical Velocity v(x) along Horizontal Centerline")
ax2.set_xlabel("x"); ax2.set_ylabel("v(x, 0.5)"); ax2.legend(); ax2.grid(True)
plt.savefig("fig7_cavity_re_profiles.png", dpi=300)
plt.close()

print("All 7 figures generated successfully!")