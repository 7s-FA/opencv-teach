import * as T from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
import {GLTFLoader} from 'three/addons/loaders/GLTFLoader.js';
import {STLLoader} from 'three/addons/loaders/STLLoader.js';
import {jointAngles} from './math.js';
const vec=(s,defaultValue='0 0 0')=>(s||defaultValue).split(/\s+/).map(Number);
function origin(g,e) {if(!e)return;g.position.fromArray(vec(e.getAttribute('xyz')));g.rotation.set(...vec(e.getAttribute('rpy')),'ZYX');}
export async function makeScene(host,data) {
  const scene=new T.Scene();scene.background=new T.Color('#dfe7ed');
  const camera=new T.PerspectiveCamera(45,1,.005,10);camera.up.set(0,0,1);
  const renderer=new T.WebGLRenderer({antialias:true});renderer.setPixelRatio(Math.min(devicePixelRatio,2));host.append(renderer.domElement);renderer.toneMapping=T.ACESFilmicToneMapping;renderer.toneMappingExposure=.85;
  renderer.domElement.setAttribute('aria-label','실제 SO-101 모델의 스텝 자세. 드래그하여 회전, 휠로 확대합니다.');
  const controls=new OrbitControls(camera,renderer.domElement);controls.enableDamping=true;controls.minDistance=.3;controls.maxDistance=2.8;
  function reset(){camera.position.set(.902,-.512,1.11);controls.target.set(.14,.25,.14);controls.update();}reset();
  scene.add(new T.HemisphereLight(0xffffff,0x7b8890,1.5));const sun=new T.DirectionalLight(0xffffff,2);sun.position.set(-1,1,3);scene.add(sun);
  const environment=await new GLTFLoader().loadAsync('./workcell.glb');
  environment.scene.traverse(n=>{if(n.isMesh){n.material.roughness=.9;n.material.metalness=.05;}});
  scene.add(environment.scene);
  const xml=await fetch('./robot/so101_modified.urdf').then(r=>{if(!r.ok)throw Error('로봇 모델을 불러오지 못했습니다.');return r.text();});
  const doc=new DOMParser().parseFromString(xml,'application/xml');
  const loader=new STLLoader(),cache=new Map();
  const robot=new T.Group(),links={},joints={};
  for(const el of doc.querySelectorAll('robot > link')) {
    const link=new T.Group();link.name=el.getAttribute('name');links[link.name]=link;
    for(const visual of el.querySelectorAll(':scope > visual')) {
      const mesh=visual.querySelector('mesh');if(!mesh)continue;
      const url='./robot/'+mesh.getAttribute('filename');if(!cache.has(url))cache.set(url,loader.loadAsync(url));
      const geometry=await cache.get(url);const rgba=vec(visual.querySelector('color')?.getAttribute('rgba'),'0.12 0.16 0.18 1');
      const model=new T.Mesh(geometry,new T.MeshStandardMaterial({color:new T.Color(...rgba.slice(0,3)),roughness:.72,metalness:.12}));
      origin(model,visual.querySelector('origin'));if(mesh.getAttribute('scale'))model.scale.fromArray(vec(mesh.getAttribute('scale')));link.add(model);
    }
  }
  const children=new Set();
  for(const el of doc.querySelectorAll('robot > joint')) {
    const parent=el.querySelector('parent').getAttribute('link'),child=el.querySelector('child').getAttribute('link');children.add(child);
    const base=new T.Group(),turn=new T.Group();origin(base,el.querySelector('origin'));base.add(turn);turn.add(links[child]);links[parent].add(base);
    if(el.getAttribute('type')!=='fixed')joints[el.getAttribute('name')]={node:turn,axis:new T.Vector3(...vec(el.querySelector('axis')?.getAttribute('xyz'),'0 0 1'))};
  }
  for(const [name,link] of Object.entries(links))if(!children.has(name))robot.add(link);
  const robots={};
  for(const arm of ['arm2','arm3']) {
    const copy=robot.clone(true);const transform=data.arms[arm].world.flat();transform[3]/=1000;transform[7]/=1000;transform[11]/=1000;
    copy.applyMatrix4(new T.Matrix4().set(...transform));scene.add(copy);
    // clone node lookup by the stable traversal order of each joint pivot.
    const originals=[],copies=[];robot.traverse(n=>originals.push(n));copy.traverse(n=>copies.push(n));
    robots[arm]={copy,joints:Object.fromEntries(Object.entries(joints).map(([name,j])=>[name,{node:copies[originals.indexOf(j.node)],axis:j.axis}]))};
  }
  let dirty=true;controls.addEventListener('change',()=>{dirty=true;});
  const observer=new ResizeObserver(()=>{const w=host.clientWidth,h=host.clientHeight;if(!w||!h)return;renderer.setSize(w,h);camera.aspect=w/h;camera.updateProjectionMatrix();dirty=true;});observer.observe(host);
  let stopped=false;function frame(){if(stopped)return;requestAnimationFrame(frame);if(!host.offsetParent)return;controls.update();if(dirty){renderer.render(scene,camera);dirty=false;}}frame();
  return {setPose(arm,ticks){dirty=true;const angles=jointAngles(ticks,data.arms[arm]);data.joints.forEach((name,i)=>robots[arm].joints[name]?.node.quaternion.setFromAxisAngle(robots[arm].joints[name].axis,angles[i]));},reset,dispose(){stopped=true;observer.disconnect();controls.dispose();renderer.dispose();}};
}
