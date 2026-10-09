import test from 'node:test';import assert from 'node:assert/strict';import {jigTransform} from '../src/math.js';
test('rotates a taught TCP around its jig and keeps height',()=>{const p=jigTransform([220,100,30],[200,100,0],[210,95,30]);assert.ok(Math.abs(p[0]-227.320508)<1e-5);assert.ok(Math.abs(p[1]-105)<1e-9);assert.equal(p[2],30);});
test('square jig crossing 89 to 1 follows nearest +2 degree rotation',()=>{const p=jigTransform([20,0,7],[0,0,89],[0,0,1],90);assert.ok(Math.abs(p[0]-20*Math.cos(2*Math.PI/180))<1e-9);assert.ok(p[1]>0&&p[1]<1);});
