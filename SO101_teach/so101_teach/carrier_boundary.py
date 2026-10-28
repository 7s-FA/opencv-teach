"""Joint physical rectangle fit to measured partial rim segments."""
import numpy as np
from scipy.optimize import least_squares

def fit_observed(fit,xy,measured,observed_along,profile,z):
    from .vision import world_xy
    def rejected(reason,**values):return None,{'reason':reason,**values}
    # Fit one constrained rectangle to measured samples, preserving their side identities.
    initial=np.asarray(fit['xy']);target=np.asarray(xy)
    choices=[np.roll(initial,k,axis=0) for k in range(4)]+[np.roll(initial[::-1],k,axis=0) for k in range(4)]
    initial=min(choices,key=lambda v:np.sum((v-target)**2));origin=fit['center'];local=(initial-origin)/fit['scale']
    observed_xy=[world_xy(p,profile,z) for p in measured]
    normals=np.roll(local,-1,axis=0)-local;normals=np.c_[-normals[:,1],normals[:,0]];normals/=np.linalg.norm(normals,axis=1,keepdims=True)
    def joint_residual(delta,masks=None):
        angle=np.deg2rad(delta[2]);R=np.array([[np.cos(angle),-np.sin(angle)],[np.sin(angle),np.cos(angle)]])
        residual=[]
        for i,p in enumerate(observed_xy):
            take=np.ones(len(p),bool) if masks is None else masks[i]
            canonical=(p[take]-origin-delta[:2])@R
            residual.extend(((canonical-local[i]*delta[3])@normals[i]).tolist())
        return np.asarray(residual)
    initial_delta=np.array([0.,0.,0.,fit['scale']]);bounds=([-2,-2,-1,.97],[2,2,1,1.03])
    solve=lambda masks=None:least_squares(lambda d:joint_residual(d,masks),initial_delta,bounds=bounds,loss='huber',f_scale=.5,max_nfev=40,ftol=1e-7,xtol=1e-7,gtol=1e-7)
    joint=solve();residual=joint_residual(joint.x)
    if np.quantile(abs(residual),.95)>1.2:return rejected('observed_joint_residual',p95_mm=float(np.quantile(abs(residual),.95)))
    differences=[]
    for block in range(4):
        masks=[np.minimum((a*4).astype(int),3)!=block for a in observed_along]
        if any(np.count_nonzero(m)<12 for m in masks):return rejected('observed_insufficient_holdout')
        leave=solve(masks);differences.append([float(np.linalg.norm(leave.x[:2]-joint.x[:2])),float(abs(leave.x[2]-joint.x[2]))])
    uncertainty=np.max(differences,axis=0)
    if uncertainty[0]>.5 or uncertainty[1]>.5:return rejected('observed_joint_instability',holdout_max_mm_deg=uncertainty.tolist())
    angle=np.deg2rad(joint.x[2]);R=np.array([[np.cos(angle),-np.sin(angle)],[np.sin(angle),np.cos(angle)]])
    fit['xy']=local@R.T*joint.x[3]+origin+joint.x[:2];fit['center']=origin+joint.x[:2];fit['scale']=joint.x[3]
    return fit,{'observed_joint_residual_p95_mm':float(np.quantile(abs(residual),.95)),'observed_joint_holdout_max_mm_deg':uncertainty.tolist(),'observed_joint':True}
