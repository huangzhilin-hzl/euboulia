import assert from 'node:assert/strict';
import test from 'node:test';
import {filterHotspots, estimate, hitTest, explain} from '../../src/euboulia/web/profile-model.mjs';

test('filters preserve rank denominators and sort without mutating evidence',()=>{
 const rows=[{name:'GEMM',rank:'0',kind:'gpu_kernel',total_ns:60,count:1,share:.6},{name:'GEMM',rank:'1',kind:'gpu_kernel',total_ns:90,count:3,share:.9},{name:'launch',rank:'0',kind:'cuda_api',total_ns:10,count:10,share:1}];
 assert.deepEqual(filterHotspots(rows,{rank:'0',kind:'gpu_kernel',search:'gemm'}),[rows[0]]);
 assert.equal(filterHotspots(rows,{search:'gemm'})[0].share,.9);
 assert.equal(filterHotspots(rows,{sort:'count'})[0].name,'launch');
 assert.equal(rows[0].total_ns,60);
 assert.deepEqual(filterHotspots(rows,{search:'missing'}),[]);
});
test('critical-path assumptions obey Amdahl bounds',()=>{
 assert.deepEqual(estimate(0,5),{latencyReduction:0,speedup:1});
 assert.deepEqual(estimate(1,2),{latencyReduction:.5,speedup:2});
 assert.ok(Math.abs(estimate(.2,2).latencyReduction-.1)<1e-12);
 assert.equal(estimate(.8,1).latencyReduction,0);
 for(const [f,s] of [[-1,2],[1.1,2],[.2,0],[NaN,2],[.2,Infinity]])assert.throws(()=>estimate(f,s));
});
test('hover picks the visually topmost overlapping event and ignores gaps',()=>{
 const rects=[{x:100,y:20,w:50,h:22,event:{id:1}},{x:110,y:20,w:10,h:22,event:{id:2}}];
 assert.deepEqual(hitTest(rects,115,30),{id:2});
 assert.deepEqual(hitTest(rects,150,42),{id:1});
 assert.equal(hitTest(rects,151,30),null);
 assert.equal(hitTest(rects,110,43),null);
 assert.match(explain({kind:'gpu_kernel',count:5})[1],/NCU/);
});
