export function jigTransform(point, oldPose, newPose, symmetry=360) {
  const angle = ((newPose[2]-oldPose[2]+symmetry/2)%symmetry+symmetry)%symmetry-symmetry/2;
  const r = angle*Math.PI/180, x=point[0]-oldPose[0], y=point[1]-oldPose[1];
  return [newPose[0]+Math.cos(r)*x-Math.sin(r)*y,newPose[1]+Math.sin(r)*x+Math.cos(r)*y,point[2]];
}
export function jointAngles(ticks, arm) {
  return ticks.map((tick,i)=>{
    const points=Object.values(arm.mapping)[i];
    const zero=points[1], end=tick<zero[0]?points[0]:points[2];
    return arm.offsets[i]+(zero[1]+(tick-zero[0])*(end[1]-zero[1])/(end[0]-zero[0]))*Math.PI/180;
  });
}
