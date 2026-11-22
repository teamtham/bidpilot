"""Founder dashboard page: a direct build of the approved wireframe (v5).

The layout, wording and states follow docs/wireframe/founder-dashboard-wireframe.html.
All data comes from /api/state and every action goes through the existing
owner routes, so the outbound check and the approval gate are unchanged.
"""
import json

CSS = r"""
:root{--bg:#f2f4f3;--card:#ffffff;--ink:#17201d;--mute:#5d6a65;--line:#dde2df;--soft:#eef1ef;--teal:#0e6b5b;--teal-ink:#ffffff;--teal-soft:#dff0eb;--red:#b3261e;--red-soft:#fce9e7;--amber:#7a4e00;--amber-soft:#fff2cf;--green:#1b6b3d;--green-soft:#e0f3e7;--blue:#1f5a99;--blue-soft:#e1edf9;--j1:#7a97c4;--j2:#c9a45c;--j3:#be7f95;--shadow:0 1px 2px rgba(20,30,26,.06);--font:'Instrument Sans',system-ui,-apple-system,'Segoe UI',sans-serif}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#101513;--card:#171e1b;--ink:#e6ece9;--mute:#9aa7a1;--line:#27312d;--soft:#1e2723;--teal:#3dae96;--teal-ink:#07201a;--teal-soft:#16332d;--red:#ff9d93;--red-soft:#3b1d1a;--amber:#f0c36a;--amber-soft:#3a2d10;--green:#7ad79f;--green-soft:#173424;--blue:#8dbcf0;--blue-soft:#162a40;--j1:#5f7fb0;--j2:#a8854a;--j3:#a56379;--shadow:none;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#101513;--card:#171e1b;--ink:#e6ece9;--mute:#9aa7a1;--line:#27312d;--soft:#1e2723;--teal:#3dae96;--teal-ink:#07201a;--teal-soft:#16332d;--red:#ff9d93;--red-soft:#3b1d1a;--amber:#f0c36a;--amber-soft:#3a2d10;--green:#7ad79f;--green-soft:#173424;--blue:#8dbcf0;--blue-soft:#162a40;--j1:#5f7fb0;--j2:#a8854a;--j3:#a56379;--shadow:none;color-scheme:dark}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--ink);font:14px/1.5 var(--font);padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));padding-block:0}
button,input,select,textarea{font:inherit;color:inherit}
button{cursor:pointer}
:focus-visible{outline:2px solid var(--teal);outline-offset:2px}
h1,h2,h3,h4,p{margin:0}
.top{display:flex;align-items:center;gap:16px;padding:12px 0;border-bottom:1px solid var(--line);flex-wrap:wrap}
.logo{display:flex;align-items:center;gap:8px;font-weight:700;font-size:16px}
.logo i{width:20px;height:20px;border-radius:6px;background:var(--teal);display:block}
.tabs{display:flex;gap:4px}
.tab{border:0;background:transparent;padding:7px 12px;border-radius:6px;color:var(--mute);font-weight:500}
.tab[aria-current="page"]{background:var(--teal-soft);color:var(--teal);font-weight:600}
.tab:hover{color:var(--ink)}
.sp{flex:1}
.pill{font-size:12px;border:1px solid var(--line);border-radius:999px;padding:3px 10px;color:var(--mute);background:var(--card)}
.btn{border:1px solid var(--line);background:var(--card);border-radius:6px;padding:8px 14px;font-weight:600;min-height:36px}
.btn:hover{border-color:var(--mute)}
.btn.pri{background:var(--teal);border-color:var(--teal);color:var(--teal-ink)}
.btn.pri:disabled{opacity:.5;cursor:not-allowed}
.btn.sm{padding:5px 10px;min-height:30px;font-size:13px}
.btn.danger{color:var(--red)}
.link{border:0;background:none;color:var(--teal);font-weight:600;padding:2px 0;text-decoration:underline;text-underline-offset:2px}
.railbtn{display:none}
.shell{display:grid;grid-template-columns:272px minmax(0,1fr);gap:24px;padding-block:20px 56px}
.shell.wide{grid-template-columns:minmax(0,1fr)}
.rail{display:flex;flex-direction:column;gap:8px;align-self:start;position:sticky;top:12px;max-height:calc(100vh - 24px);overflow:auto;padding-right:4px}
.rail h2{font-size:12px;font-weight:600;color:var(--mute);text-transform:uppercase;letter-spacing:.06em;margin:10px 0 2px;display:flex;justify-content:space-between}
.card-deal{display:flex;flex-direction:column;gap:3px;align-items:flex-start;text-align:left;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;box-shadow:var(--shadow);width:100%}
.card-deal b{font-weight:600}
.card-deal .sub{font-size:12px;color:var(--mute)}
.card-deal[aria-current="true"]{border-color:var(--teal);box-shadow:0 0 0 1px var(--teal)}
.card-deal.test{opacity:.65}
.chip{display:inline-block;font-size:12px;font-weight:600;border-radius:999px;padding:2px 9px;background:var(--soft);color:var(--mute);white-space:nowrap}
.chip.ok{background:var(--green-soft);color:var(--green)}.chip.warn{background:var(--amber-soft);color:var(--amber)}.chip.bad{background:var(--red-soft);color:var(--red)}.chip.info{background:var(--blue-soft);color:var(--blue)}
.week{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;font-size:13px}
.meter{height:6px;border-radius:999px;background:var(--soft);margin:6px 0;overflow:hidden}
.meter i{display:block;height:100%;background:var(--teal)}
.main{display:flex;flex-direction:column;gap:16px;min-width:0}
.head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap}
.head h1{font-size:26px;line-height:1.2;font-weight:600}
.meta{color:var(--mute);font-size:13px;margin-top:4px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;box-shadow:var(--shadow);min-width:0}
.panel>h3,.ph{font-size:13px;font-weight:600;color:var(--mute);margin-bottom:10px;display:flex;justify-content:space-between;gap:8px;align-items:center}
.figs{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.fig span{display:block;font-size:12px;color:var(--mute)}
.fig b{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums}
.fig small{font-size:11px;color:var(--mute);font-weight:500;margin-left:3px}
.dim .figs{opacity:.45}
.bar{height:8px;border-radius:999px;background:var(--soft);margin:12px 0 6px;position:relative;overflow:hidden}
.bar i{display:block;height:100%;background:var(--teal)}.bar.bad i{background:var(--red)}
.nb{position:relative;height:12px;border-radius:999px;background:var(--soft);margin:12px 0 6px;overflow:hidden;border:1px solid var(--line)}
.nb i{position:absolute;top:0;bottom:0;display:block}
.nbu{left:0;background:var(--teal)}
.nbo{background:repeating-linear-gradient(45deg,var(--red) 0 4px,transparent 4px 8px);opacity:.85}
.nbm{width:0;border-left:2px dashed var(--ink)}
.nbl{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--mute);margin-bottom:4px}
.nbl .over{color:var(--red)}
.wstrip{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:4px 0 8px}
.wchip{border:1px solid var(--line);border-radius:999px;padding:2px 10px;font-size:12px;background:var(--card)}
.wchip.tight{border-color:var(--red);color:var(--red)}
.note{font-size:12px;color:var(--mute)}
.warns{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px;margin-top:12px}
.warn{display:flex;gap:10px;background:var(--red-soft);color:var(--red);border-radius:8px;padding:10px 12px}
.warn b{display:block;font-weight:600}.warn span{font-size:13px}
.warn .bang{flex:none;width:20px;height:20px;border-radius:50%;background:var(--red);color:var(--card);font-weight:700;font-size:12px;display:grid;place-items:center}
.stale{background:var(--amber-soft);color:var(--amber);border-radius:10px;padding:14px 16px;display:grid;gap:8px}
.stale h3{font-size:15px;font-weight:600}
.stale ul{margin:0;padding-left:18px;display:grid;gap:4px}
.stale .acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:4px}
.stale .btn{background:var(--card);color:var(--ink)}
.stale .btn.pri{background:var(--teal);color:var(--teal-ink)}
.okline{display:flex;gap:8px;align-items:center;font-size:13px;color:var(--green);background:var(--green-soft);border-radius:8px;padding:8px 12px}
.said{background:var(--soft);border-radius:10px;padding:12px 16px}
.said q{display:block;font-size:15px;quotes:none}
.cols{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;align-items:start}
.opt{border:1px solid var(--line);border-radius:8px;padding:10px 12px;display:grid;gap:4px;margin-bottom:8px}
.opt.nofit{background:var(--soft)}
.opt .r1{display:flex;justify-content:space-between;gap:10px;align-items:baseline}
.opt label{display:flex;gap:8px;align-items:flex-start;font-weight:600}
.opt input{margin-top:3px;accent-color:var(--teal);width:16px;height:16px}
.opt .d{color:var(--mute);font-size:13px;margin-left:24px}
.opt .pr{font-weight:600;font-variant-numeric:tabular-nums}
.opt .r3{margin-left:24px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:12px}
.fit-ok{color:var(--green)}.fit-no{color:var(--red)}
.sim{display:flex;gap:12px;align-items:center;border:1px solid var(--line);border-radius:8px;padding:10px}
.thumb{flex:none;width:72px;height:48px;border-radius:6px;background:linear-gradient(135deg,var(--j1),var(--j3));display:grid;place-items:center;color:#fff;font-size:12px}
.draft{white-space:pre-wrap;background:var(--soft);border-radius:8px;padding:12px;font-size:14px}
textarea{width:100%;min-height:150px;border:1px solid var(--line);border-radius:8px;padding:10px;background:var(--card);resize:vertical}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}
.chk{display:flex;gap:10px;border-radius:8px;padding:9px 12px;margin-bottom:8px;background:var(--soft)}
.chk .ic{flex:none;font-weight:700}
.chk b{display:block;font-weight:600}.chk span{font-size:13px}
.chk.pass{background:var(--green-soft);color:var(--green)}.chk.block{background:var(--red-soft);color:var(--red)}
.lock{font-size:13px;color:var(--mute);margin-top:8px}
.confirm{border:1px solid var(--teal);border-radius:8px;padding:12px;margin-top:10px;background:var(--teal-soft)}
.sent{border:1px solid var(--green);background:var(--green-soft);color:var(--green);border-radius:10px;padding:14px 16px;display:grid;gap:6px}
.sent code{background:var(--card);color:var(--ink);padding:2px 6px;border-radius:4px;font-size:12px}
.col{border:1px solid var(--line);border-radius:10px;background:var(--card)}
.col>button{width:100%;text-align:left;border:0;background:none;padding:12px 16px;font-weight:600;display:flex;justify-content:space-between}
.col .body{padding:0 16px 14px;display:grid;gap:6px;font-size:13px}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
th{font-weight:600;color:var(--mute);font-size:12px}
td.n,th.n{text-align:right}
.miss{color:var(--red);font-weight:600}
.tl{list-style:none;margin:0;padding:0;display:grid;gap:8px}
.tl li{display:grid;grid-template-columns:120px 1fr;gap:10px}.tl time{color:var(--mute);font-size:12px}
.field{display:grid;gap:4px;font-size:13px;font-weight:600}
.field input,.field select{border:1px solid var(--line);border-radius:6px;padding:8px 10px;background:var(--card);min-width:0}
.fgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
/* calendar */
.cal{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:16px;align-items:start}
.chart{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;align-items:end}
.wk{display:grid;gap:4px;justify-items:stretch}
.stack{height:170px;display:flex;flex-direction:column-reverse;border-bottom:2px solid var(--line);border-radius:4px 4px 0 0;position:relative;background:repeating-linear-gradient(0deg,transparent 0 33px,var(--line) 33px 34px)}
.stack>div{flex:none}
.stack .sp2{flex:1}
.seg1{background:var(--j1)}.seg2{background:var(--j2)}.seg3{background:var(--j3)}
.segb{background:repeating-linear-gradient(45deg,var(--soft) 0 5px,var(--line) 5px 10px);border:1px dashed var(--mute)}
.segp{background:var(--teal);opacity:.92;background-image:repeating-linear-gradient(45deg,rgba(255,255,255,.25) 0 4px,transparent 4px 8px)}
.wk.hi .stack{box-shadow:0 0 0 3px var(--amber-soft),0 0 0 4px var(--amber)}
.wk .lb{font-size:12px;font-weight:600;text-align:center}.wk .fr{font-size:11px;color:var(--mute);text-align:center}
.wk .due{font-size:11px;text-align:center;color:var(--red);font-weight:600;min-height:16px}
.over{color:var(--red)!important;font-weight:600}
.legend{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--mute);margin-top:10px}
.legend i{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:5px;vertical-align:-2px}
.hbars{display:grid;gap:4px}
.hrow{display:grid;grid-template-columns:96px minmax(0,1fr) 130px;gap:12px;align-items:center;padding:4px 0}
.hrow.hi .hbar{box-shadow:0 0 0 3px var(--amber-soft),0 0 0 4px var(--amber)}
.hl b{display:block;font-weight:600}.hl span{font-size:12px;color:var(--mute)}
.hbar{height:34px;position:relative;border:1px solid var(--line);border-radius:6px;overflow:hidden;background:var(--card)}
.hbar.isover{border-color:var(--red)}
.hbar>div{position:absolute;top:0;bottom:0;display:flex;align-items:center;padding:0 8px;font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;color:var(--ink)}
.hbar>.capl{padding:0;width:0;border-left:2px dashed var(--ink);z-index:3}
.hbar>.ov{padding:0;z-index:2;background:repeating-linear-gradient(45deg,var(--red) 0 4px,transparent 4px 8px);opacity:.75;border-left:0}
.hbar .segp{color:var(--teal-ink)}
.hbar .segb{color:var(--mute);font-weight:500;border:0;border-left:1px dashed var(--mute)}
.hr{font-size:13px;display:grid;gap:2px}
.hr .due{color:var(--red);font-weight:600;font-size:12px}
.hr .over{color:var(--red);font-weight:600}
@media (max-width:900px){.hrow{grid-template-columns:78px minmax(0,1fr)}.hr{grid-column:2}}
td.hi{background:var(--amber-soft);color:var(--amber);font-weight:600}
.tw{overflow-x:auto}
.side{display:grid;gap:16px}
.tools{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.tool{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;display:grid;gap:6px}
.tgroup{display:grid;gap:10px}
.tgroup>h2{font-size:12px;font-weight:600;color:var(--mute);text-transform:uppercase;letter-spacing:.06em}
.tgrid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
@media (max-width:900px){.tgrid{grid-template-columns:minmax(0,1fr)}}
.tool{text-align:left;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;display:grid;grid-template-columns:44px minmax(0,1fr);gap:4px 14px;box-shadow:var(--shadow);align-content:start}
.tool:hover{border-color:var(--teal)}
.tool .ico{grid-row:span 3;width:44px;height:44px;border-radius:10px;background:var(--teal-soft);color:var(--teal);display:grid;place-items:center}
.tool .ico svg{width:22px;height:22px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.tool b{font-size:15px;font-weight:600}
.tool .ds{font-size:13px;color:var(--mute)}
.tool .ft{display:flex;gap:8px;align-items:center;justify-content:space-between;margin-top:6px}
.tool .go{color:var(--teal);font-weight:600;font-size:13px}
.pgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}
.pcard{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;box-shadow:var(--shadow)}
.pcard .pt{aspect-ratio:16/9;display:grid;place-items:center;color:#fff;font-size:13px;background:linear-gradient(135deg,var(--j1),var(--j3))}
.pcard .pt.b2{background:linear-gradient(135deg,var(--j2),var(--j1))}.pcard .pt.b3{background:linear-gradient(135deg,var(--j3),var(--j2))}
.pcard .pb{padding:12px;display:grid;gap:6px}
.fchips{display:flex;gap:8px;flex-wrap:wrap}
.fchip{border:1px solid var(--line);background:var(--card);border-radius:999px;padding:5px 12px;font-weight:500;font-size:13px}
.fchip[aria-pressed="true"]{background:var(--teal);border-color:var(--teal);color:var(--teal-ink)}
.crumb{font-size:13px}
.sheet{position:fixed;top:0;right:0;bottom:0;width:min(520px,100%);background:var(--card);border-left:1px solid var(--line);box-shadow:-8px 0 24px rgba(0,0,0,.18);z-index:30;overflow:auto;padding:20px max(16px,env(safe-area-inset-right,0px)) 32px 20px;display:grid;gap:14px;align-content:start}
.qty{width:72px;border:1px solid var(--line);border-radius:6px;padding:6px 8px;background:var(--card)}
.sheet .opt{margin:0}
.toast{position:fixed;left:50%;transform:translateX(-50%);bottom:max(16px,env(safe-area-inset-bottom,0px));background:var(--ink);color:var(--bg);padding:10px 16px;border-radius:8px;z-index:40;max-width:calc(100% - 32px);font-size:13px}
.notes li{margin-bottom:6px}
.notes h3{margin:12px 0 6px;font-size:13px;color:var(--mute)}
@media (max-width:900px){
 .shell{grid-template-columns:minmax(0,1fr)}
 .rail{display:none;position:static;max-height:none}
 .rail.open{display:flex}
 .railbtn{display:inline-block}
 .cols,.cal{grid-template-columns:minmax(0,1fr)}
 .figs{grid-template-columns:repeat(2,minmax(0,1fr))}
 .chart{gap:4px}
 .tl li{grid-template-columns:1fr}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}

/* live-page additions */
.tool{color:inherit;font:inherit;cursor:pointer}
.pcard .pt{cursor:pointer}.pcard .pt:disabled{cursor:default;opacity:.85}
button:disabled{cursor:not-allowed}
"""

JS = r"""
(function(){
var OWNER=new URLSearchParams(location.search).get('owner')||'';
var MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
var DAY=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
function D(s){return new Date(s+'T00:00:00Z')}
function iso(d){return d.toISOString().slice(0,10)}
function addDays(s,n){var d=D(s);d.setUTCDate(d.getUTCDate()+n);return iso(d)}
function lab(s){var d=D(s);return d.getUTCDate()+' '+MON[d.getUTCMonth()]}
function n1(x){return (Math.round(x*10)/10).toFixed(1)}
function n2(x){return Number(x).toFixed(2)}
function commas(n,dec){var p=Number(n).toFixed(dec).split('.');return p[0].replace(/\B(?=(\d{3})+(?!\d))/g,',')+(p[1]?'.'+p[1]:'')}
function money(x,dec){return 'S$'+commas(x,dec?2:0)}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&'+'amp;','<':'&'+'lt;','>':'&'+'gt;','"':'&'+'quot;',"'":'&'+'#39;'}[c]})}
var NW=['zero','one','two','three','four','five','six','seven','eight','nine','ten'];
function when(ts){if(!ts)return '';var d=new Date(ts*1000+8*3600e3);var h=d.getUTCHours(),m=d.getUTCMinutes();return DAY[d.getUTCDay()]+' '+d.getUTCDate()+' '+MON[d.getUTCMonth()]+', '+(h<10?'0':'')+h+':'+(m<10?'0':'')+m}
function monday(weekEnding){return addDays(weekEnding,-4)}
var SRC={hashmeta:'Hashmeta form',site:'Hashmeta chat',manual:'Added by you'};
var FMT={product_promo:'promo video',social_reel:'social reel',testimonial:'testimonial',event_recap:'event recap',explainer:'explainer',brand_film:'brand film'};
var FMTCHIP={product_promo:'Product promo',social_reel:'Social reel',testimonial:'Testimonial',event_recap:'Event recap',explainer:'Explainer',brand_film:'Brand film'};
var FIELD={'offer.budget_sgd':'Budget','budget_sgd':'Budget','offer.deadline':'Deadline','deadline':'Deadline','offer.deliverables':'Videos','offer.caption_versions':'Captioned versions','offer.interviewees':'Interviewees','offer.direct_costs_sgd':'Direct costs','offer.client':'Client name','offer.format':'Format'};
var IC={"grid":"<svg viewBox=\"0 0 24 24\" aria-hidden=\"true\"><rect x=\"3\" y=\"3\" width=\"7\" height=\"7\" rx=\"1.5\"/><rect x=\"14\" y=\"3\" width=\"7\" height=\"7\" rx=\"1.5\"/><rect x=\"3\" y=\"14\" width=\"7\" height=\"7\" rx=\"1.5\"/><rect x=\"14\" y=\"14\" width=\"7\" height=\"7\" rx=\"1.5\"/></svg>","tag":"<svg viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path d=\"M3 12V4h8l10 10-8 8z\"/><circle cx=\"7.5\" cy=\"8.5\" r=\"1.2\"/></svg>","doc":"<svg viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path d=\"M6 3h8l4 4v14H6z\"/><path d=\"M14 3v4h4M9 12h6M9 16h6\"/></svg>","chat":"<svg viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path d=\"M4 5h16v11H9l-5 4z\"/></svg>","tool":"<svg viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path d=\"M14 6a4 4 0 0 0 5 5l-9 9a2.1 2.1 0 0 1-3-3l9-9z\"/></svg>","list":"<svg viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path d=\"M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01\"/></svg>"};

var ST=null,SNAP='';
var S={tab:'deals',sub:null,deal:null,rail:false,notes:false,showTest:false,prev:null,hi:null,confirm:null,toast:'',open:{},win:0,pf:'all',sim:{},tick:{},drafts:{},check:{},qty:{},busy:false,more:{}};

/* ---------- server ---------- */
async function api(path,data){
 try{var r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Owner-Token':OWNER},body:JSON.stringify(data||{})});
  var x=await r.json();x._status=r.status;return x}
 catch(e){return {ok:false,message:'The dashboard could not reach BidPilot. Check that it is running, then try again.'}}}
async function refresh(){await refreshState(false)}
var pollTimer=null,liveUpdates=true;
async function refreshState(auto){
 if(auto&&(document.hidden||S.busy))return;
 var r;try{r=await fetch('/api/state',{headers:{'X-Owner-Token':OWNER}})}catch(e){return}
 if(r.status!==200){document.getElementById('app').innerHTML='<div class="shell wide"><div class="main"><section class="panel"><h3>Could not load your deals</h3><p>Open the dashboard from your owner link on this Mac.</p></section></div></div>';return}
 var st=await r.json(),snap=JSON.stringify(st);
 if(auto&&snap===SNAP)return;
 if(auto&&busyEditing())return;
 ST=st;SNAP=snap;build();render()}
function busyEditing(){var a=document.activeElement;if(a&&a.matches&&a.matches('textarea,input,select'))return true;return !!(S.confirm||S.prev||S.notes)}
function schedulePolling(){clearInterval(pollTimer);if(liveUpdates&&!document.hidden)pollTimer=setInterval(function(){refreshState(true)},4000)}
document.addEventListener('visibilitychange',function(){schedulePolling();if(!document.hidden)refreshState(true)});

/* ---------- view models ---------- */
var DEALS={},ORDER=[],WEEKS=[],JOBS=[];
function latest(d){return d.versions.length?d.versions[d.versions.length-1]:null}
function liveFree(w){return Math.max(0,w.remaining_hours)}
function weeksUpTo(by){return WEEKS.filter(function(w){return monday(w.week)<=by})}
function build(){
 DEALS={};ORDER=[];
 WEEKS=(ST.capacity&&ST.capacity.weeks)||[];
 var byClient={},c=0;JOBS=[];
 ((ST.capacity&&ST.capacity.jobs)||[]).forEach(function(j){var k=j.deal_id;if(!byClient[k]){byClient[k]={id:k,client:j.client,c:(c++%3)+1,h:{},deal:k};JOBS.push(byClient[k])}byClient[k].h[j.week]=(byClient[k].h[j.week]||0)+j.hours});
 ST.deals.forEach(function(raw){var d=model(raw);DEALS[d.id]=d;ORDER.push(d.id)});
 if(!S.deal||!DEALS[S.deal]){var first=ORDER.map(function(k){return DEALS[k]}).filter(function(d){return ['review','counter','info','estimate'].indexOf(d.phase)>-1})[0]||DEALS[ORDER[0]];S.deal=first?first.id:null}}
function model(raw){
 var v=latest(raw),off=(raw.offers.length?raw.offers[raw.offers.length-1].offer:null)||{},d={raw:raw,v:v,id:raw.deal_id,client:raw.client};
 var kind=FMT[off.format]||(off.format?String(off.format).replace(/_/g,' '):'video');
 var what=off.deliverables?(off.deliverables+' '+kind+(off.deliverables===1?'':'s')):'';
 d.title=raw.client+(v&&v.version_no>1?', version '+v.version_no:(what?', '+what:''));
 d.src=SRC[raw.source]||raw.source;d.rcv=when(raw.created_at);
 d.ask=off.deliverables?{videos:off.deliverables,by:off.deadline,price:off.budget_sgd,text:(NW[off.deliverables]||off.deliverables)+' video'+(off.deliverables===1?'':'s')+' by '+(off.deadline?lab(off.deadline):'the deadline')}:null;
 var p=raw.phase;
 if(raw.is_test)d.phase='test';
 else if(p==='counter_ready')d.phase='counter';
 else if(p==='ready_for_review')d.phase='review';
 else if(p==='draft_blocked'||p==='needs_fresh_estimate')d.phase=(v&&v.verdict==='counter')?'counter':'review';
 else if(p==='needs_info')d.phase=(raw.needs_information&&raw.needs_information.question_sent)?'qsent':'info';
 else if(p==='needs_estimate'||p==='new')d.phase='estimate';
 else if(p==='sent'||p==='client_picked'||p==='change_requested')d.phase='sent';
 else if(p==='booked')d.phase='booked';
 else d.phase='rejected';
 d.stale=!!raw.needs_fresh_estimate&&(d.phase==='review'||d.phase==='counter');
 var e=v&&v.estimate;
 if(e&&d.ask){var all=(v.capacity_changes&&v.capacity_changes.length)?v.capacity_changes.map(function(x){return {week:x.week,free:Math.max(0,x.before)}}):weeksUpTo(d.ask.by).map(function(w){return {week:w.week,free:liveFree(w)}});
  d.est={need:e.planning_hours,free:e.capacity_hours,fit:['accept','accept_with_approval'].indexOf(v.verdict)>-1,list:e.list_sgd,lowest:e.floor_sgd,rate:e.planning_hours?e.list_sgd/e.planning_hours:0,past:(v.comparables&&v.comparables.used||[]).length,buffer:e.buffer_pct==null?15:e.buffer_pct,all:all}}
 var inbound=raw.messages.filter(function(m){return m.direction==='inbound'});
 d.said=inbound.length?inbound[inbound.length-1].body:null;
 var meta={};(v&&v.option_meta||[]).forEach(function(m){meta[m.option_id]=m});
 d.opts=(v&&v.options||[]).map(function(o){var m=meta[o.option_id]||{},met=(v.option_metrics||{})[o.option_id]||((v.options.length===1&&v.estimate)?v.estimate:null),desc;
  if((o.phases||[]).length>1)desc=o.phases.map(function(ph,i){return ph.deliverables+(i?' more':' video'+(ph.deliverables===1?'':'s'))+' by '+lab(ph.deadline)}).join(', ');
  else desc=o.deliverables+' video'+(o.deliverables===1?'':'s')+' by '+lab(o.deadline);
  var need=met?met.planning_hours:null,cap=met?met.capacity_hours:null,ok=need==null||need<=cap+1e-9;
  return {id:m.option_row_id||o.option_id,key:o.option_id,name:m.label||o.option_id,desc:desc,price:o.price_sgd,ok:ok,left:need==null?null:cap-need,short:need==null?0:need-cap,by:(o.phases&&o.phases.length?o.phases[o.phases.length-1].deadline:o.deadline),terms:o}});
 if(raw.needs_information){d.missing=(raw.needs_information.missing_fields||[]).map(function(f){return FIELD[f]||String(f).replace(/^offer\./,'').replace(/_/g,' ')}).filter(function(x,i,a){return a.indexOf(x)===i});d.question=raw.needs_information.question||''}
 d.brief=briefRows(raw,off,d);
 return d}
function briefRows(raw,off,d){
 var ch={};var v=latest(raw);((v&&v.comparison&&v.comparison.changed_fields)||[]).forEach(function(f){ch[f.field]=f.old});
 function was(k,fmt){return ch[k]!=null?' (was '+fmt(ch[k])+')':''}
 var miss=function(name){return (d.missing||[]).indexOf(name)>-1};
 var rows=[['Videos',off.deliverables!=null?off.deliverables+was('deliverables',String):null],['Captioned versions',off.caption_versions!=null?off.caption_versions+was('caption_versions',String):null],['Deadline',off.deadline?lab(off.deadline)+was('deadline',lab):null],['Budget',(off.budget_sgd!=null&&!miss('Budget'))?'About '+money(off.budget_sgd)+was('budget_sgd',function(x){return money(x)}):null]];
 return rows}

/* ---------- calendar maths (live) ---------- */
function weekIdx(){var today=new Date(Date.now()+8*3600e3).toISOString().slice(0,10);for(var i=0;i<WEEKS.length;i++)if(WEEKS[i].week>=today)return i;return 0}
function blocksFor(w){return ((ST.capacity&&ST.capacity.blocks)||[]).filter(function(b){return b.week===w})}
function hoursSetting(){return (ST.capacity&&ST.capacity.working_hours)||(WEEKS[0]&&WEEKS[0].working_hours)||40}
function freeBefore(by){var t=0;weeksUpTo(by).forEach(function(w){t+=liveFree(w)});return t}

/* ---------- status ---------- */
function stat(d){switch(d.phase){
 case 'counter':return d.stale?['Needs fresh estimate','warn']:['Counter ready','warn'];
 case 'review':return d.stale?['Needs fresh estimate','warn']:(d.v&&d.v.outbound_status==='blocked'?['Draft blocked','bad']:['Ready for review','warn']);
 case 'info':return ['Needs info','warn'];case 'estimate':return ['Needs your estimate','warn'];
 case 'qsent':return ['Waiting on client','info'];
 case 'sent':return d.v&&d.v.accepted_at?['Client picked an option','warn']:['Waiting on client','info'];
 case 'booked':return ['Booked','ok'];case 'rejected':return ['Rejected',''];default:return ['Test data','']}}
function headChip(d){
 if(d.phase==='counter'||d.phase==='review'){if(d.stale)return ['Needs a fresh estimate','warn'];return d.est&&d.est.fit?['Fits as asked','ok']:['Doesn’t fit as asked','bad']}
 return stat(d)}
function railCard(d){var t=stat(d);return '<button class="card-deal'+(d.phase==='test'?' test':'')+'" data-act="open" data-id="'+d.id+'"'+(S.deal===d.id?' aria-current="true"':'')+'><b>'+esc(d.client)+'</b><span class="sub">'+esc(d.rcv)+' · '+esc(d.src)+'</span><span class="chip '+t[1]+'">'+t[0]+'</span></button>'}
function rail(){
 var all=ORDER.map(function(k){return DEALS[k]});
 var real=all.filter(function(d){return d.phase!=='test'}),tests=all.filter(function(d){return d.phase==='test'});
 var need=real.filter(function(d){return ['review','counter','info','estimate'].indexOf(d.phase)>-1});
 var prog=real.filter(function(d){return ['sent','booked','qsent'].indexOf(d.phase)>-1});
 var closed=real.filter(function(d){return d.phase==='rejected'});
 var w=WEEKS[weekIdx()],f=w?liveFree(w):0,c=w?w.budget_hours:0,pct=c?Math.min(100,Math.max(0,(c-f)/c*100)):0;
 return '<nav class="rail'+(S.rail?' open':'')+'" aria-label="Deals">'
 +'<h2>This week</h2><div class="week">'+(w?'Week of '+lab(monday(w.week))+': <b>'+n1(f)+' h free</b> of '+n1(c)+' h':'No calendar weeks yet')+'<div class="meter"><i style="width:'+pct+'%"></i></div><button class="link" data-act="tab" data-tab="calendar">Open the calendar</button></div>'
 +'<h2>Needs you <span>'+need.length+'</span></h2>'+need.map(railCard).join('')
 +'<h2>In progress <span>'+prog.length+'</span></h2>'+prog.map(railCard).join('')
 +'<h2>Closed <span>'+closed.length+'</span></h2>'+closed.map(railCard).join('')
 +'<h2>Test data <button class="link" data-act="showTest">'+(S.showTest?'Hide':'Show')+' ('+tests.length+')</button></h2>'+(S.showTest?tests.map(railCard).join(''):'')+'</nav>'}

/* ---------- estimate ---------- */
function figs(d){var e=d.est;return '<div class="figs"><div class="fig"><span>Hours needed</span><b>'+n2(e.need)+' h</b></div><div class="fig"><span>Free hours before '+lab(d.ask.by)+'</span><b>'+n2(e.free)+' h</b></div><div class="fig"><span>List price</span><b>'+money(e.list,1)+'</b></div><div class="fig"><span>Lowest you’d accept</span><b>'+money(e.lowest,1)+'<small>private</small></b></div></div>'}
function need2bar(e){
 var sc=Math.max(e.need,e.free,0.0001),used=Math.min(e.need,e.free),short=e.need-e.free;
 var lbl=short>0.05?'<b class="over">'+n1(short)+' h short</b>':'<b style="color:var(--green)">'+n1(-short)+' h to spare</b>';
 return '<div class="nb" role="img" aria-label="'+n1(e.need)+' hours needed, '+n1(e.free)+' hours free"><i class="nbu" style="width:'+(used/sc*100)+'%"></i>'+(short>0.05?'<i class="nbo" style="left:'+(e.free/sc*100)+'%;width:'+(short/sc*100)+'%"></i>':'')+'<i class="nbm" style="left:'+(e.free/sc*100)+'%"></i></div><div class="nbl"><span>'+n1(e.free)+' h free</span><span>'+n1(e.need)+' h needed</span>'+lbl+'</div>'}
function weekStrip(d){
 var rows=d.est.all.filter(function(x){return monday(x.week)<=d.ask.by}),min=-1,mv=1e9;
 rows.forEach(function(x,i){if(x.free<mv){mv=x.free;min=i}});
 return '<div class="wstrip"><span class="note">Free hours by week before '+lab(d.ask.by)+':</span>'+rows.map(function(x,i){return '<span class="wchip'+(i===min&&!d.est.fit?' tight':'')+'">'+lab(monday(x.week))+': <b>'+n1(x.free)+' h</b></span>'}).join('')+'</div>'}
function estimatePanel(d){
 var e=d.est,st=d.stale,fit=e.fit;
 var title=st?'Engine estimate, private to you':(fit?'Engine estimate, private to you':'Why this needs a counter offer');
 var h='<section class="panel'+(st?' dim':'')+'"><h3><span>'+title+'</span>'+(st?'<span class="chip warn">Out of date</span>':'')+'</h3>'+figs(d)
  +need2bar(e)+weekStrip(d)
  +'<p class="note">Works out to '+money(e.rate,1)+' an hour. Based on '+e.past+' similar past job'+(e.past===1?'':'s')+', plus '+e.buffer+'% extra time for surprises.</p>';
 if(!st&&!fit&&d.phase!=='sent'){
  var w='';
  if(e.need>e.free+0.05)w+='<div class="warn"><span class="bang">!</span><div><b>Not enough time before '+lab(d.ask.by)+'</b><span>This needs about '+Math.round(e.need)+' hours. You have '+Math.round(e.free)+' hours free before then.</span></div></div>';
  if(d.ask.price!=null&&d.ask.price<e.lowest-0.5)w+='<div class="warn"><span class="bang">!</span><div><b>Price is under your minimum</b><span>'+money(d.ask.price)+' is '+money(Math.round(e.lowest-d.ask.price))+' less than the lowest you’d accept for '+(NW[d.ask.videos]||d.ask.videos)+' video'+(d.ask.videos===1?'':'s')+'.</span></div></div>';
  if(w)h+='<div class="warns">'+w+'</div>'}
 return h+'</section>'}
function reasons(d){
 var out=[],ch=(d.v&&d.v.capacity_changes)||[];
 ch.forEach(function(x){var used=x.before-x.after;if(Math.abs(used)<0.05||monday(x.week)>d.ask.by)return;
  var who=JOBS.filter(function(j){return j.h[x.week]&&j.id!==d.id}).map(function(j){return j.client});
  var b=blocksFor(x.week);
  if(used>0)out.push((who.length?'New bookings ('+who.join(', ')+')':'Bookings')+(b.length?' and blocked time':'')+' used '+n1(used)+' more hours in the week of '+lab(monday(x.week))+'.');
  else out.push(n1(-used)+' more hours are free in the week of '+lab(monday(x.week))+'.')});
 if(!out.length)out.push('Free hours changed in the weeks before your deadline.');
 return out}
function consequence(d){
 var live=freeBefore(d.ask.by),need=d.est.need;
 if(need>live+1e-9){var t='The full package ('+d.ask.text+') no longer fits: it needs '+n1(need)+' h and you have '+n1(live)+' h free.';
  var ok=d.opts.filter(function(o){return o.ok});if(ok.some(function(o){return o.key==='phased'}))t+=' Phased delivery still does.';
  if(ok.length)t+=' Options that still fit: '+ok.map(function(o){return o.name}).join(', ')+'.';return t}
 return live>d.est.free?'More time is free now. Your proposal still fits.':'Your proposal still fits, but the numbers changed. Re-run the estimate to refresh them.'}
function staleBlock(d){
 return '<section class="stale" role="alert"><h3>Your calendar changed since this estimate</h3><ul>'+reasons(d).map(function(r){return '<li>'+esc(r)+'</li>'}).join('')+'</ul><p>'+esc(consequence(d))+'</p><div class="acts"><button class="btn pri" data-act="rerun">Re-run the estimate</button><button class="btn" data-act="seecal">See it on the calendar</button></div></section>'}
function tickset(d){var k=d.v.version_id;if(!S.tick[k]){S.tick[k]={};d.opts.forEach(function(o){S.tick[k][o.id]=o.ok});if(d.v.offered_option_ids){d.opts.forEach(function(o){S.tick[k][o.id]=d.v.offered_option_ids.indexOf(o.id)>-1})}}return S.tick[k]}
function ticked(d){var t=tickset(d);return d.opts.filter(function(o){return o.ok&&t[o.id]})}
function optionsPanel(d){
 var t=tickset(d),h='<section class="panel"><h3><span>'+(d.opts.length>1?'Options for the client':'Option the client will see')+'</span></h3>';
 d.opts.forEach(function(o){
  h+='<div class="opt'+(o.ok?'':' nofit')+'"><div class="r1"><label><input type="checkbox" data-act="tick" data-opt="'+o.id+'"'+(o.ok&&t[o.id]?' checked':'')+(o.ok?'':' disabled')+'> <span>'+esc(o.name)+'</span></label><span class="pr">'+money(o.price)+'</span></div><div class="d">'+esc(o.desc)+'</div><div class="r3">'
  +(o.left==null?'':(o.ok?'<span class="fit-ok">Fits, '+n1(o.left)+' h left before '+lab(o.by)+'</span>':'<span class="fit-no">Doesn’t fit: short by '+n1(o.short)+' h before '+lab(o.by)+'</span>'))
  +'<button class="link" data-act="preview" data-opt="'+o.id+'">Preview on calendar</button></div></div>'});
 if(d.opts.length>1)h+='<p class="note">Untick any option you don’t want to offer.</p>';
 return h+'</section>'}
function simPanel(d){
 if(d.phase==='info')return '<section class="panel"><h3>Similar work to show</h3><p class="note">Not used for questions. It appears on the proposal once there is one.</p></section>';
 if(d.phase==='estimate')return '<section class="panel"><h3>Similar work to show</h3><p class="note">Appears after you run the estimate, when there is a proposal to send.</p></section>';
 var list=d.raw.similar_work||[];
 if(!list.length)return '<section class="panel"><h3>Similar work to show</h3><p class="note">Nothing in your portfolio library yet. Add work in Studio tools, Portfolio library.</p></section>';
 var s=list[(S.sim[d.id]||0)%list.length];
 return '<section class="panel"><h3>Similar work to show</h3><div class="sim"><div class="thumb" aria-hidden="true">Reel</div><div style="flex:1;min-width:0"><b>'+esc(s.title)+'</b><div class="note">'+esc(s.reason)+'</div></div>'+(list.length>1?'<button class="btn sm" data-act="sim">Change</button>':'')+'</div><p class="note" style="margin-top:8px">Shown on every proposal before it goes out.</p></section>'}

/* ---------- checks and approval ---------- */
function lockReason(d){
 if(d.phase==='counter'||d.phase==='review'){
  if(d.stale)return 'The calendar changed since this estimate. Re-run the estimate first.';
  if(d.opts.length&&!ticked(d).length)return 'Tick at least one option to offer.'}
 return ''}
function checkResult(d){
 var c=S.check[d.id];if(c)return c;var v=d.v;if(!v||v.outbound_status==='unchecked'||S.drafts[d.id]!=null)return null;
 return fromViolations(d,v.check_violations||[],v.outbound_status==='passed')}
function fromViolations(d,vs,passed){
 if(passed)return {safe:{ok:true},price:{ok:true}};
 var res={safe:{ok:true},price:{ok:true}};
 var priv=vs.filter(function(x){return ['PRIVATE_NUMBER','PRIVATE_LABEL','OTHER_CLIENT_NAME'].indexOf(x.code)>-1})[0];
 if(priv){var text=String(priv.text||''),what='something private';
  if(priv.code==='OTHER_CLIENT_NAME')what='another client’s name';
  else if(priv.code==='PRIVATE_LABEL')what='internal pricing wording';
  else if(d.est&&(text.indexOf(commas(d.est.lowest,2))>-1||text.indexOf(String(d.est.lowest))>-1))what='your minimum price';
  else if(d.est&&(text.indexOf(n2(d.est.need))>-1||text.indexOf(n2(d.est.free))>-1))what='your hours';
  var draft=String((S.drafts[d.id]!=null?S.drafts[d.id]:d.v.draft)||''),shown=draft.indexOf('S$'+text)>-1?'S$'+text:text;
  res.safe={ok:false,msg:'Blocked: this draft shows '+what,fix:priv.code==='PRIVATE_LABEL'?'Reword the internal pricing language before it can go to the client.':'Take out “'+shown+'” before it can go to the client.'}}
 var other=vs.filter(function(x){return !priv||x!==priv}).filter(function(x){return ['PRIVATE_NUMBER','PRIVATE_LABEL','OTHER_CLIENT_NAME'].indexOf(x.code)<0})[0];
 if(other)res.price={ok:false,msg:other.message||'The draft mentions something that is not one of your options.'};
 if(!vs.length&&!passed)res.price={ok:false,msg:'The check did not pass. Edit the draft and try again.'};
 return res}
function cleanDraft(d){var sig=JSON.stringify(d.v.options);var prior=d.raw.versions.filter(function(x){return x.version_no<d.v.version_no&&x.outbound_status==='passed'&&JSON.stringify(x.options)===sig});return prior.length?prior[prior.length-1].draft:null}
function checks(d){
 var c=checkResult(d),rs=lockReason(d),h='<h4 class="ph" style="margin-top:14px">Before it goes out</h4>',pend='Checked when you press Approve and send.';
 function row(x,title,ok){
  if(!c)return '<div class="chk"><span class="ic">○</span><div><b>'+title+'</b><span>'+pend+'</span></div></div>';
  if(x.ok)return '<div class="chk pass"><span class="ic">✓</span><div><b>'+title+'</b><span>'+ok+'</span></div></div>';
  return '<div class="chk block"><span class="ic">✕</span><div><b>'+esc(x.msg.indexOf('Blocked')===0?x.msg:'Not safe to send')+'</b><span>'+esc(x.fix||x.msg)+'</span></div></div>'}
 if(d.phase!=='info'){
  h+=row(c&&c.safe,'Safe to send','No hours, minimum price or other client names in the message.');
  h+=row(c&&c.price,'Price matches the estimate','Price and dates come from this estimate.');
  if(c&&!c.safe.ok&&cleanDraft(d))h+='<div class="row" style="margin-top:0"><button class="btn" data-act="clean">Use the clean draft</button></div>'}
 else h='';
 var lab2=d.phase==='info'?'Approve and send question':'Approve and send';
 if(S.confirm&&S.confirm.deal===d.id){
  var n=d.phase==='info'?0:ticked(d).length;
  h+='<div class="confirm"><b>'+(S.confirm.kind==='approve'?(d.phase==='info'?'Send this question to '+esc(d.client)+'?':'Checks passed. Send '+n+' option'+(n===1?'':'s')+' to '+esc(d.client)+'?'):'Reject this deal?')+'</b><p class="note" style="margin:4px 0 8px">'+(S.confirm.kind==='approve'?(d.phase==='info'?'The client gets your question. Nothing else goes out.':'This creates the client’s private proposal page. Only the ticked options are shown to them.'):'Nothing is sent to the client. The deal moves to Closed and keeps its history.')+'</p><div class="row" style="margin-top:0"><button class="btn pri" data-act="'+(S.confirm.kind==='approve'?'doApprove':'doReject')+'"'+(S.busy?' disabled':'')+'>'+(S.confirm.kind==='approve'?'Yes, send':'Yes, reject')+'</button><button class="btn" data-act="cancel">Cancel</button></div></div>'}
 else h+='<div class="row"><button class="btn pri" data-act="approve"'+(rs||S.busy?' disabled':'')+'>'+lab2+'</button>'+(d.phase==='info'?'':'<button class="btn danger" data-act="reject">Reject</button>')+'</div>'+(rs?'<p class="lock">Approve is locked. '+esc(rs)+'</p>':'<p class="lock">'+(d.phase==='info'?'Nothing goes out before you approve.':'Runs the checks, then creates the client’s private proposal page. Nothing goes out before you approve.')+'</p>');
 return h}
function draftText(d){if(S.drafts[d.id]!=null)return S.drafts[d.id];if(d.phase==='info')return d.question||'';return d.v?d.v.draft:''}
function draftPanel(d){
 return '<section class="panel"><h3><span>'+(d.phase==='info'?'Question for the client':'Draft message')+'</span><span class="note" style="font-weight:400">Click to edit</span></h3><textarea id="draft" aria-label="Draft message">'+esc(draftText(d))+'</textarea>'
 +(d.est&&d.est.lowest?'<div class="row" style="margin-top:6px"><button class="link note" data-act="leak" title="Demo only">Demo: add your minimum price to the draft</button></div>':'')
 +'<div id="checkpanel">'+checks(d)+'</div></section>'}
function coll(key,title,body){var o=S.open[key];return '<div class="col"><button data-act="toggle" data-key="'+key+'" aria-expanded="'+(!!o)+'"><span>'+title+'</span><span>'+(o?'−':'+')+'</span></button>'+(o?'<div class="body">'+body+'</div>':'')+'</div>'}
function briefHtml(d){return '<table><tbody>'+d.brief.map(function(r){return '<tr><th scope="row">'+r[0]+'</th><td>'+(r[1]==null?'<span class="miss">Missing</span>':esc(r[1]))+'</td></tr>'}).join('')+'</tbody></table>'}
var STEP={claimed:'Brief received',resume:'Brief received again',intake:'Intake saved',scoping:'Similar past jobs picked',engine:'Estimate made',engine_evaluate:'Estimate made',reevaluate:'Estimate re-run',draft:'Draft written',edit_draft:'You edited the draft',outbound:'Draft checked',outbound_check:'Draft checked',review:'Waiting for your review',approve:'Proposal sent',reject:'You rejected this',client_change:'Client asked for a change',client_accept:'Client picked an option',capacity_revalidate:'Calendar re-checked',rotate_token:'Client link reissued',confirm_booking:'You confirmed the booking',close_deal:'Deal closed',needs_information:'Checked what was missing',stale_version:'Change was for an older version',error:'Something went wrong'};
function stepLine(s){
 var t=STEP[s.step_key]||String(s.step_key).replace(/_/g,' '),n='';
 if(s.step_key==='approve'&&s.status!=='ok'){t='Approve refused';n='The check or the calendar stopped it.'}
 else if((s.step_key==='outbound_check'||s.step_key==='outbound')&&s.status==='blocked'){t='Draft blocked by the check'}
 else if(s.step_key==='outbound_check'&&s.status==='passed'){t='Draft passed the check'}
 else if(s.step_key==='needs_information'){var j=null;try{j=JSON.parse(s.private_note)}catch(e){}if(j&&j.question_sent){t='Question sent';n=j.question}else if(j&&j.missing_fields){n='Missing: '+j.missing_fields.map(function(f){return FIELD[f]||f}).join(', ')+'.'}}
 else if(s.step_key==='engine_evaluate'&&s.status==='running'){t='Estimate started'}
 else if(s.step_key==='engine_evaluate'){var m=/verdict=(\w+)/.exec(s.private_note||'');if(m)n={accept:'Fits the calendar.',counter:'Doesn’t fit as asked, so options were made.',clarify:'Needs your estimate.',decline:'Doesn’t fit at all.'}[m[1]]||''}
 return [when(s.finished_at||s.started_at),t,n]}
function histHtml(d){
 var steps=d.raw.steps.slice().reverse(),show=S.more[d.id]?steps:steps.slice(0,8);
 var rows=show.map(stepLine).map(function(r){return '<li><time>'+esc(r[0])+'</time><span><b>'+esc(r[1])+'</b>'+(r[2]?'<br><span class="note">'+esc(r[2])+'</span>':'')+'</span></li>'}).join('');
 var total=d.raw.steps_total||steps.length;
 var more=total>show.length?'<p class="note">Showing the latest '+show.length+' of '+total+' steps. Repeated checks that found nothing new are no longer logged. <button class="link" data-act="more">Show earlier</button></p>':'';
 return '<ul class="tl">'+rows+'</ul>'+more}
function head(d){var c=headChip(d);return '<div class="head"><div><h1>'+esc(d.title)+'</h1><p class="meta">'+esc(d.src)+' · received '+esc(d.rcv)+'</p></div><span class="chip '+c[1]+'">'+c[0]+'</span></div>'}
function approvalAt(d,action){var a=(d.raw.approvals||[]).filter(function(x){return x.action===action});return a.length?when(a[a.length-1].decided_at):''}
function sentCard(d){
 var v=d.v,url=v&&v.client_url?location.origin+v.client_url:'';
 var picked=v&&v.accepted_option&&v.accepted_option.terms&&!v.confirmed;
 return '<section class="sent"><b>Sent to '+esc(d.client)+'</b><span>Their private proposal page is ready.</span>'+(url?'<code>'+esc(url)+'</code><div class="row" style="margin-top:4px"><button class="btn pri" data-act="page">Go to page</button><button class="btn" data-act="copy">Copy link</button></div>':'<span class="note">The link was created before links were stored. Reissue it from History if you need it.</span>')
 +(picked?'<span><b>'+esc(d.client)+' picked '+esc(optLabel(v.accepted_option.terms))+'.</b></span><div class="row" style="margin-top:0"><button class="btn pri" data-act="confirmBooking">Confirm booking</button></div>':'')
 +'<span>Booking only becomes final when you confirm it.</span></section>'}
function optLabel(t){var m=(latestMeta()[t.option_id])||{};var desc=(t.phases&&t.phases.length>1)?t.phases.map(function(p,i){return p.deliverables+(i?' more':' video'+(p.deliverables===1?'':'s'))+' by '+lab(p.deadline)}).join(', '):t.deliverables+' videos by '+lab(t.deadline);return (m.label||t.option_id)+': '+desc+', '+money(t.price_sgd)}
function latestMeta(){var d=DEALS[S.deal],m={};((d&&d.v&&d.v.option_meta)||[]).forEach(function(x){m[x.option_id]=x});return m}
function estimateForm(d){
 var rates=ST.price_list||[],q=S.qty[d.id];
 if(!q){q={};var off=(d.raw.offers.length?d.raw.offers[d.raw.offers.length-1].offer:{})||{};
  if(off.graphics==='custom_animation'){q.anim=off.deliverables||1;q.edit=off.deliverables||0;q.cap=off.caption_versions||0}else{q.promo=off.deliverables||1;q.cap=off.caption_versions||0}S.qty[d.id]=q}
 var rows=rates.map(function(r){return '<tr><th scope="row" style="color:var(--ink)">'+esc(r.name)+'<div class="note" style="font-weight:400">'+esc(r.unit)+'</div></th><td class="n">'+n1(r.hours)+' h</td><td class="n">'+money(r.price_sgd)+'</td><td class="n"><input class="qty" id="q-'+r.id+'" type="number" min="0" step="1" value="'+(q[r.id]||0)+'" aria-label="Quantity of '+esc(r.name)+'"></td></tr>'}).join('');
 var off2=(d.raw.offers.length?d.raw.offers[d.raw.offers.length-1].offer:{})||{},why=off2.graphics==='custom_animation'?'custom animation':'this kind of job';
 return '<section class="panel"><h3>This job needs your estimate</h3><p>The engine can’t price '+why+' by itself, so I built a starting point from your price list. Change the quantities, then run the estimate.</p><div class="tw" style="margin-top:10px"><table><thead><tr><th>Item</th><th class="n">Hours each</th><th class="n">Price each</th><th class="n">Quantity</th></tr></thead><tbody>'+rows+'</tbody></table></div><p id="esttotals" style="margin-top:10px">'+totalsHtml(readTotals(d))+'</p><div class="row"><button class="btn pri" data-act="runEst"'+(S.busy?' disabled':'')+'>Run estimate</button><button class="link" data-act="rates">Edit your price list</button></div>'+(d.ask?'<p class="note" style="margin-top:8px">Brief: '+esc(d.ask.text)+(d.ask.price!=null?', budget about '+money(d.ask.price):'')+'.</p>':'')+'</section>'}
function readTotals(d){var h=0,p=0,q=S.qty[d.id]||{};(ST.price_list||[]).forEach(function(r){var el=document.getElementById('q-'+r.id),n=el?(parseFloat(el.value)||0):(q[r.id]||0);h+=n*r.hours;p+=n*r.price_sgd});return {h:h,p:p}}
function totalsHtml(t){return 'Total: <b>'+n1(t.h)+' h</b> and <b>'+money(t.p)+'</b>'}

function dealView(d){
 if(!d)return '<div class="main"><section class="panel"><h3>No deals yet</h3><p>New Hashmeta inquiries appear here after QwenWork claims them.</p></section></div>';
 var h='<div class="main">'+head(d);
 if(d.phase==='test')return h+'<section class="panel"><h3>Test data</h3><p>This deal comes from an evaluation run. It is kept out of your queue and your calendar.</p><div class="row"><button class="btn" data-act="showTest">Hide test data</button></div></section></div>';
 if(d.phase==='booked')return h+'<section class="sent" style="background:var(--green-soft)"><b>Booked</b><span>You confirmed this on '+esc(approvalAt(d,'confirm_booking'))+'. The hours are set aside on your calendar.</span>'+(d.v&&d.v.accepted_option&&d.v.accepted_option.terms?'<span>Client accepted: '+esc(optLabel(d.v.accepted_option.terms))+'.</span>':'')+'<div class="row" style="margin-top:4px"><button class="btn" data-act="tab" data-tab="calendar">Open on the calendar</button></div></section>'+coll('hist','History',histHtml(d))+'</div>';
 if(d.phase==='rejected')return h+'<section class="panel"><h3>Rejected</h3><p>You rejected this on '+esc(approvalAt(d,'reject')||d.rcv)+'. Nothing was sent to the client. It stays here for history and no longer counts in your queue.</p></section>'+coll('hist','History',histHtml(d))+'</div>';
 if(d.phase==='info')return h+'<section class="panel"><h3>I need a bit more before I can price this</h3><p>Missing: <b class="miss">'+esc((d.missing||[]).join(' and ')||'details not recorded')+'</b>.</p><div style="margin-top:10px">'+briefHtml(d)+'</div></section><div class="cols">'+draftPanel(d)+simPanel(d)+'</div>'+coll('hist','History',histHtml(d))+'</div>';
 if(d.phase==='qsent')return h+'<section class="sent"><b>Question sent to '+esc(d.client)+'</b><span>It comes back to your queue when they reply with their '+esc((d.missing||[]).join(' and ').toLowerCase()||'details')+'.</span></section>'+coll('hist','History',histHtml(d))+'</div>';
 if(d.phase==='estimate')return h+estimateForm(d)+simPanel(d)+coll('brief','Brief',briefHtml(d))+'</div>';
 if(d.phase==='sent')h+=sentCard(d);
 if(d.said)h+='<section class="said"><span class="note">Client said</span><q>“'+esc(d.said)+'”</q><span class="note">Client text. Treated as a request, not an approval.</span></section>';
 if(d.phase==='sent')return h+(d.est?estimatePanel(d):'')+coll('brief','Brief',briefHtml(d))+coll('hist','History',histHtml(d))+'</div>';
 h+=d.stale?staleBlock(d):'<div class="okline"><span>✓</span><span>Calendar checked. Nothing has changed since this estimate. <button class="link" data-act="tab" data-tab="calendar">Open the calendar</button></span></div>';
 if(d.est)h+=estimatePanel(d);
 h+='<div class="cols"><div style="display:grid;gap:16px">'+optionsPanel(d)+simPanel(d)+'</div>'+draftPanel(d)+'</div>';
 h+=coll('brief','Brief',briefHtml(d))+coll('hist','History',histHtml(d));
 return h+'</div>'}

/* ---------- calendar ---------- */
function chart(idxs,o){
 var base=hoursSetting(),scale=base,rowsD=[];
 idxs.forEach(function(i){var w=WEEKS[i];if(!w)return;var bk=0;JOBS.forEach(function(j){bk+=j.h[w.week]||0});var p=(o.prev&&o.prev[w.week])||0;rowsD.push({i:i,w:w,bk:bk,p:p});if(bk+p>scale)scale=bk+p});
 var pc=function(v){return (v/scale*100)+'%'},h='<div class="hbars">';
 rowsD.forEach(function(r){
  var blocked=blocksFor(r.w.week).reduce(function(t,b){return t+b.hours},0),cap=Math.max(0,r.w.budget_hours),segs='',x=0,tot=r.bk+r.p,over=tot-cap,hi=S.hi&&S.hi.weeks.indexOf(r.w.week)>-1;
  if(blocked>0){var why=blocksFor(r.w.week)[0];segs+='<div class="segb" style="left:'+pc(Math.max(0,base-blocked))+';width:'+pc(blocked)+'">'+(blocked/scale>0.14?esc(why.reason)+' '+n1(blocked)+' h':'')+'</div>'}
  JOBS.forEach(function(j){var v=j.h[r.w.week]||0;if(!v)return;segs+='<div class="seg'+j.c+'" style="left:'+pc(x)+';width:'+pc(v)+'" title="'+esc(j.client)+'">'+(v/scale>0.17?esc(j.client.split(' ')[0])+' '+n1(v)+' h':'')+'</div>';x+=v});
  if(r.p){segs+='<div class="segp" style="left:'+pc(x)+';width:'+pc(r.p)+'">'+(r.p/scale>0.15?'This option '+n1(r.p)+' h':'')+'</div>';x+=r.p}
  if(over>0.05)segs+='<div class="ov" style="left:'+pc(cap)+';width:'+pc(over)+'"></div>';
  segs+='<div class="capl" style="left:'+pc(cap)+'"></div>';
  var m=monday(r.w.week);
  h+='<div class="hrow'+(hi?' hi':'')+'"><div class="hl"><b>'+lab(m)+'</b><span>to '+lab(addDays(m,6))+'</span></div><div class="hbar'+(over>0.05?' isover':'')+'">'+segs+'</div><div class="hr">'+(over>0.05?'<span class="over">Over by '+n1(over)+' h</span><span class="note">'+n1(tot)+' h booked, '+n1(cap)+' h available</span>':'<span>'+n1(cap-tot)+' h '+(r.p?'left':'free')+'</span>')+(o.dl&&o.dl[r.w.week]?'<span class="due">Due '+o.dl[r.w.week]+'</span>':'')+'</div></div>'});
 return h+'</div>'}
function legend(extra){return '<div class="legend">'+JOBS.map(function(j){return '<span><i class="seg'+j.c+'"></i>'+esc(j.client)+'</span>'}).join('')+'<span><i class="segb"></i>Blocked</span>'+(extra?'<span><i class="segp"></i>This option</span>':'')+'</div>'}
function windowIdx(){var start=Math.max(0,Math.min(WEEKS.length-6,weekIdx()+S.win)),out=[];for(var i=start;i<start+6&&i<WEEKS.length;i++)out.push(i);return out}
function calView(){
 var idxs=windowIdx(),aff=ORDER.map(function(k){return DEALS[k]}).filter(function(d){return d.stale});
 if(!idxs.length)return '<div class="main"><section class="panel"><h3>Studio calendar</h3><p>No calendar weeks yet.</p></section></div>';
 var first=monday(WEEKS[idxs[0]].week),last=addDays(monday(WEEKS[idxs[idxs.length-1]].week),6);
 var h='<div class="main"><div class="head"><div><h1>Studio calendar</h1><p class="meta">Booked hours per job, per week. BidPilot plans in hours, so there are no invented time slots.</p></div><div class="row" style="margin:0"><button class="btn sm" data-act="win" data-d="-1" aria-label="Previous week">‹</button><button class="btn sm" data-act="win" data-d="0">Today</button><button class="btn sm" data-act="win" data-d="1" aria-label="Next week">›</button></div></div>';
 h+='<div class="cal"><div style="display:grid;gap:16px;min-width:0"><section class="panel"><h3><span>'+lab(first)+' to '+lab(last)+'</span><span>Working hours: '+n1(hoursSetting()).replace('.0','')+' h a week</span></h3><p class="note" style="margin-bottom:8px">Each bar is one week of your working hours. Coloured blocks are booked jobs, hatched is time you blocked, and the empty part is free. The dashed line is your capacity for the week, and red stripes are hours over it.</p>'+chart(idxs,{})+legend(false)+'</section>';
 h+='<section class="panel tw"><h3>Hours per job</h3><table><thead><tr><th>Job</th>'+idxs.map(function(i){return '<th class="n">'+lab(monday(WEEKS[i].week))+'</th>'}).join('')+'</tr></thead><tbody>';
 JOBS.forEach(function(j){var dl=DEALS[j.deal];h+='<tr><th scope="row" style="color:var(--ink)">'+(dl?'<button class="link" data-act="open" data-id="'+j.deal+'">'+esc(j.client)+'</button>':esc(j.client))+'<div class="note" style="font-weight:400">'+esc(dl?dl.title.replace(j.client+', ',''):'Booked job')+'</div></th>'+idxs.map(function(i){var w=WEEKS[i].week,v=j.h[w],hi=S.hi&&S.hi.weeks.indexOf(w)>-1&&v;return '<td class="n'+(hi?' hi':'')+'">'+(v?n1(v):'–')+'</td>'}).join('')+'</tr>'});
 h+='<tr><th scope="row">Blocked</th>'+idxs.map(function(i){var b=blocksFor(WEEKS[i].week).reduce(function(t,x){return t+x.hours},0);return '<td class="n">'+(b?n1(b):'–')+'</td>'}).join('')+'</tr><tr><th scope="row">Free</th>'+idxs.map(function(i){return '<td class="n"><b>'+n1(liveFree(WEEKS[i]))+'</b></td>'}).join('')+'</tr></tbody></table></section></div>';
 var bl=(ST.capacity&&ST.capacity.blocks)||[];
 h+='<div class="side"><section class="panel"><h3>Working hours</h3><label class="field">Hours a week<input id="hrs" type="number" min="1" max="80" value="'+hoursSetting()+'"></label><div class="row"><button class="btn sm pri" data-act="saveHrs">Save</button></div></section>';
 h+='<section class="panel"><h3>Block time</h3><div style="display:grid;gap:10px"><label class="field">Week<select id="bw">'+idxs.map(function(i){return '<option value="'+WEEKS[i].week+'">Week of '+lab(monday(WEEKS[i].week))+'</option>'}).join('')+'</select></label><label class="field">Hours<input id="bh" type="number" min="1" max="80" value="8"></label><label class="field">Reason<select id="br"><option>Leave</option><option>Admin</option><option>Outside booking</option></select></label></div><div class="row"><button class="btn sm pri" data-act="addBlock">Block it</button></div>';
 h+='<ul class="tl" style="margin-top:12px">'+bl.map(function(b){return '<li style="grid-template-columns:1fr auto;align-items:center"><span>'+esc(b.reason)+', '+n1(b.hours)+' h, week of '+lab(monday(b.week))+'</span><button class="link" data-act="delBlock" data-id="'+esc(b.id)+'">Remove</button></li>'}).join('')+'</ul></section>';
 h+='<section class="panel"><h3>Proposals affected</h3>'+(aff.length?'<ul class="tl">'+aff.map(function(d){return '<li style="grid-template-columns:1fr"><span><button class="link" data-act="open" data-id="'+d.id+'">'+esc(d.client)+'</button> needs a fresh estimate.</span></li>'}).join('')+'</ul>':'<p class="note">Every open proposal still fits the calendar as it is now.</p>')+'</section></div></div></div>';
 return h}
function sheet(){
 if(!S.prev)return '';
 var d=DEALS[S.prev.deal],o=d.opts.filter(function(x){return x.id===S.prev.opt})[0],pv=S.prev.data;
 if(!o)return '';
 if(!pv)return '<aside class="sheet" role="dialog" aria-label="Preview on calendar"><div class="head"><div><h2 style="font-size:18px;font-weight:600">Preview: '+esc(o.name)+'</h2><p class="meta">Working it out…</p></div><button class="btn sm" data-act="closePrev">Close</button></div></aside>';
 var rows={},dl={},idxs=[];(pv.weeks||[]).forEach(function(w){rows[w.week]=Math.max(0,w.before-w.after)});
 var bys=(o.terms.phases&&o.terms.phases.length?o.terms.phases.map(function(p){return p.deadline}):[o.terms.deadline]);
 WEEKS.forEach(function(w,i){bys.forEach(function(b){if(monday(w.week)<=b&&b<=addDays(monday(w.week),6))dl[w.week]=lab(b)})});
 var keys=Object.keys(rows);WEEKS.forEach(function(w,i){if(keys.indexOf(w.week)>-1||(i>=weekIdx()&&idxs.length<6))idxs.push(i)});idxs=idxs.slice(0,6);
 var fits=pv.ok!==false&&pv.fits!==false;
 var tbl='<table><thead><tr><th>Week</th><th class="n">Booked</th><th class="n">This option</th><th class="n">Free after</th></tr></thead><tbody>';
 idxs.forEach(function(i){var w=WEEKS[i],b=0;JOBS.forEach(function(j){b+=j.h[w.week]||0});var p=rows[w.week]||0;tbl+='<tr><td>'+lab(monday(w.week))+'</td><td class="n">'+n1(b)+'</td><td class="n">'+(p?n1(p):'–')+'</td><td class="n">'+n1(Math.max(0,w.budget_hours)-b-p)+'</td></tr>'});
 tbl+='</tbody></table>';
 var conflicts=(pv.affected||[]).map(function(a){return a.client}).join(', ');
 return '<aside class="sheet" role="dialog" aria-label="Preview on calendar"><div class="head"><div><h2 style="font-size:18px;font-weight:600">Preview: '+esc(o.name)+'</h2><p class="meta">'+esc(o.desc)+', '+money(o.price)+'</p></div><button class="btn sm" data-act="closePrev" aria-label="Close preview">Close</button></div>'
 +(fits?'<div class="okline"><span>✓</span><span><b>Fits.</b> '+(o.left!=null?n1(o.left)+' h will still be free before '+lab(o.by)+'.':'It fits your calendar.')+'</span></div>':'<div class="warn" style="display:block"><div><b>Doesn’t fit.</b> <span>'+esc(pv.error||('Short by '+n1(o.short)+' h before '+lab(o.by)+'.'))+'</span></div></div>')
 +(conflicts?'<p class="note">Booking this would stop other proposals fitting: '+esc(conflicts)+'.</p>':'')
 +chart(idxs,{prev:rows,dl:dl})+legend(true)+'<div class="tw">'+tbl+'</div><p class="note">Preview only. Nothing is added to your calendar, and hours promised to other clients are not touched.</p><div class="row" style="margin-top:0"><button class="btn" data-act="tab" data-tab="calendar">Open the full calendar</button></div></aside>'}

/* ---------- studio tools ---------- */
function toolsView(){
 if(S.sub==='rates')return ratesView();
 if(S.sub==='portfolio')return portfolioView();
 if(S.sub==='demo')return demoView();
 if(S.sub==='log')return logView();
 var G=[['Your work',[['portfolio','grid','Portfolio library','The work you show clients. Filter by format, see what buyers can view.','Restyled'],['rates','tag','Production estimates','Your price list: hours and price for each kind of work. Custom jobs are built from it.','Restyled']]],
  ['Clients',[['proposals','doc','Proposals and amendments','Every proposal sent, what the client picked, and change requests.','Unchanged'],['inquiry','chat','Buyer inquiry','The client chat and brief card, as clients see it.','Unchanged']]],
  ['System',[['demo','tool','Demo tools','Reset sample data, inbox and capacity tables. Moved off every deal view.','Moved here'],['log','list','Run log','Every agent step with the latest first, for when you need the detail.','New']]]];
 return '<div class="main"><div class="head"><div><h1>Studio tools</h1><p class="meta">Everything that is not a deal or the calendar.</p></div></div>'+G.map(function(g){return '<section class="tgroup"><h2>'+g[0]+'</h2><div class="tgrid">'+g[1].map(function(t){return '<button class="tool" data-act="tool" data-tool="'+t[0]+'"><span class="ico">'+IC[t[1]]+'</span><b>'+t[2]+'</b><span class="ds">'+t[3]+'</span><span class="ft"><span class="chip'+(t[4]==='Unchanged'?'':' info')+'">'+t[4]+'</span><span class="go">Open ›</span></span></button>'}).join('')+'</div></section>'}).join('')+'</div>'}
function crumb(t){return '<div class="crumb"><button class="link" data-act="tab" data-tab="tools">Studio tools</button> › '+t+'</div>'}
function ratesView(){
 var rows=(ST.price_list||[]).map(function(r){return '<tr><th scope="row" style="color:var(--ink)">'+esc(r.name)+'<div class="note" style="font-weight:400">'+esc(r.unit)+'</div></th><td class="n"><input class="qty" id="rh-'+r.id+'" type="number" min="0" step="0.5" value="'+r.hours+'" aria-label="Hours for '+esc(r.name)+'"></td><td class="n"><input class="qty" id="rp-'+r.id+'" type="number" min="0" step="10" value="'+r.price_sgd+'" aria-label="Price for '+esc(r.name)+'"></td></tr>'}).join('');
 return '<div class="main"><div class="head"><div><h1>Production estimates</h1><p class="meta">Your price list. The engine prices standard jobs from it, and custom jobs are built from it.</p></div><button class="btn sm" data-act="tab" data-tab="tools">Back to Studio tools</button></div><section class="panel tw"><table><thead><tr><th>Kind of work</th><th class="n">Hours</th><th class="n">Price (S$)</th></tr></thead><tbody>'+rows+'</tbody></table><div class="row"><button class="btn pri" data-act="saveRates">Save price list</button></div><p class="note" style="margin-top:8px">Changes apply to new estimates. Proposals you have already sent keep their price.</p></section></div>'}
function portfolioView(){
 var f=S.pf||'all',items=(ST.portfolio||[]).map(function(i,k){var fmt=FMTCHIP[i.format]||(i.format?String(i.format).replace(/_/g,' '):'Other');var size=(i.deliverables?i.deliverables+' video'+(i.deliverables===1?'':'s'):'')+(i.shoot_days!=null?(i.shoot_days?', '+i.shoot_days+' shoot day'+(i.shoot_days===1?'':'s'):', edit only'):'');return {t:i.title,fmt:fmt,b:'b'+((k%3)+1),size:size,vis:i.shared?'Buyers can see':'Private',id:i.id,shared:i.shared}});
 var fmts=[];items.forEach(function(i){if(fmts.indexOf(i.fmt)<0)fmts.push(i.fmt)});
 var buyers=items.filter(function(i){return i.shared}).length;
 var shown=items.filter(function(i){return f==='all'||(f==='buyers'&&i.shared)||(f===i.fmt)});
 var chips=[['all','All ('+items.length+')']].concat(fmts.map(function(x){return [x,x]})).concat([['buyers','Buyers can see ('+buyers+')']]).map(function(c){return '<button class="fchip" data-act="pf" data-f="'+esc(c[0])+'" aria-pressed="'+(f===c[0])+'">'+esc(c[1])+'</button>'}).join('');
 return '<div class="main">'+crumb('Portfolio library')+'<div class="head"><div><h1>Portfolio library</h1><p class="meta">The work you can show clients. Similar work on a proposal is picked from here.</p></div><button class="btn pri" data-act="addWork">Add work</button></div><div class="fchips" role="group" aria-label="Filter">'+chips+'</div>'+(shown.length?'<div class="pgrid">'+shown.map(function(i){return '<article class="pcard"><button class="pt '+i.b+'" style="border:0;width:100%" data-act="play" data-id="'+esc(i.id)+'"'+(i.shared?'':' disabled')+'>'+(i.shared?'▶ Play':'Private')+'</button><div class="pb"><b>'+esc(i.t)+'</b><div class="fchips"><span class="chip">'+esc(i.fmt)+'</span>'+(i.size?'<span class="chip">'+esc(i.size)+'</span>':'')+'<span class="chip '+(i.shared?'ok':'')+'">'+i.vis+'</span></div></div></article>'}).join('')+'</div>':'<section class="panel"><p>Nothing in this category yet.</p></section>')+'</div>'}
function demoView(){
 var fx=FIXTURES.map(function(f,i){return '<button class="btn sm" onclick="importFixture('+i+')">'+esc(f.label)+'</button>'}).join('');
 var inbox=(ST.inbox||[]).map(function(i){return '<li style="grid-template-columns:1fr"><span><b>'+(i.claimed?'Claimed':'Unclaimed')+'</b> '+esc(i.source_id)+': '+esc((i.fields||{}).name_business||'')+'</span></li>'}).join('');
 var caps=WEEKS.map(function(w){return '<tr><td>'+lab(monday(w.week))+'</td><td class="n">'+n1(w.working_hours)+'</td><td class="n">'+n1(w.budget_hours)+'</td><td class="n">'+n1(w.allocated_hours)+'</td><td class="n">'+n1(liveFree(w))+'</td></tr>'}).join('');
 return '<div class="main">'+crumb('Demo tools')+'<div class="head"><div><h1>Demo tools</h1><p class="meta">Sample data controls. They are kept off the deal views.</p></div></div>'
 +'<section class="panel"><h3>Load a sample inquiry</h3><div class="row" style="margin-top:0">'+fx+'</div><p class="note" id="intake-msg" role="status"></p></section>'
 +'<section class="panel"><h3>Import a run</h3><label class="field">Run JSON<textarea id="run-json"></textarea></label><div class="row"><button class="btn sm" data-act="importRun">Import run</button></div><p class="note" id="run-status" role="status"></p></section>'
 +'<section class="panel"><h3>Reset sample data</h3><label class="field">Type RESET SAMPLE DATA<input id="reset-confirm"></label><div class="row"><button class="btn sm danger" data-act="resetSample">Reset sample data</button></div><p class="note" id="reset-status" role="status"></p></section>'
 +'<section class="panel"><h3>Inbound inbox</h3><ul class="tl">'+(inbox||'<li>No inquiries yet.</li>')+'</ul></section>'
 +'<section class="panel tw"><h3>Planning capacity</h3><table><thead><tr><th>Week</th><th class="n">Working</th><th class="n">Available</th><th class="n">Booked</th><th class="n">Free</th></tr></thead><tbody>'+caps+'</tbody></table></section></div>'}
function logView(){
 var all=[];ORDER.forEach(function(k){var d=DEALS[k];d.raw.steps.forEach(function(s){all.push({d:d,s:s})})});
 all.sort(function(a,b){return (b.s.finished_at||b.s.started_at||0)-(a.s.finished_at||a.s.started_at||0)});
 return '<div class="main">'+crumb('Run log')+'<div class="head"><div><h1>Run log</h1><p class="meta">Every agent step, latest first.</p></div></div><section class="panel"><ul class="tl">'+all.slice(0,200).map(function(x){var l=stepLine(x.s);return '<li><time>'+esc(l[0])+'</time><span><b>'+esc(x.d.client)+': '+esc(l[1])+'</b>'+(l[2]?'<br><span class="note">'+esc(l[2])+'</span>':'')+'</span></li>'}).join('')+'</ul></section></div>'}
function notesPanel(){
 return '<aside class="sheet notes" role="dialog" aria-label="What is new in this design"><div class="head"><h2 style="font-size:18px;font-weight:600">What’s new in this design</h2><button class="btn sm" data-act="notes">Close</button></div>'
 +'<h3>Fixes from the audit</h3><ul><li><b>Stale estimate.</b> A deal whose calendar changed shows what changed, which proposal it affects and whether the date still works. Re-run the estimate, and Approve stays locked until you do.</li><li><b>No edit or check buttons.</b> The draft is always editable. The checks run when you press Approve and send, and tell you exactly what to take out.</li><li><b>Go to page and Copy link</b> appear as soon as the proposal page is ready.</li><li><b>Plain refusals.</b> Locked buttons say why in a sentence, never a code.</li><li><b>Honest status.</b> Booked and rejected deals no longer show warnings.</li><li><b>Needs-info deals</b> list exactly what is missing.</li><li><b>Custom jobs</b> are built from your price list in Studio tools, Production estimates.</li><li><b>Similar work</b> appears on every proposal before it goes out. Questions to the client and unpriced jobs say why it is not there yet.</li><li><b>One set of option names</b> everywhere. Duplicates are merged.</li><li><b>Queue.</b> Test data is tucked away, and every card has a date.</li><li><b>History</b> is capped, with no field names or skill names. Demo tools moved to Studio tools.</li><li><b>Studio tools</b> is a proper hub: grouped cards with icons, Portfolio library and Production estimates.</li><li><b>Phone.</b> The deals list becomes a menu.</li><li><b>Accessibility.</b> A real page heading, current-deal marking, and nothing under 12px.</li></ul><h3>New: the studio calendar</h3><ul><li>One bar per week, labelled with the week, the jobs and the hours free.</li><li><b>Preview on calendar</b> beside every option. It changes nothing.</li><li>Working hours, plus blocks for leave, admin or outside work.</li><li>Change anything and every affected proposal flags itself. Try blocking a week.</li></ul><h3>Not in this version</h3><ul><li>Google Calendar sync, and day-level time slots.</li></ul></aside>'}
function proposalSheet(){return ''}

/* ---------- render ---------- */
function render(){
 if(!ST){document.getElementById('app').innerHTML='<p class="note" style="padding:20px 0">Loading deals…</p>';return}
 var y=window.scrollY,d=DEALS[S.deal];
 var tab=function(id,t){return '<button class="tab" data-act="tab" data-tab="'+id+'"'+(S.tab===id?' aria-current="page"':'')+'>'+t+'</button>'};
 var h='<header class="top"><span class="logo"><i></i>BidPilot</span><nav class="tabs" aria-label="Sections">'+tab('deals','Deals')+tab('calendar','Calendar')+tab('tools','Studio tools')+'</nav><span class="sp"></span><button class="btn sm railbtn" data-act="rail">Deals</button><span class="pill">'+esc(ST.badge||'')+'</span><button class="btn sm" data-act="notes">What’s new</button></header>';
 var main=S.tab==='deals'?dealView(d):S.tab==='calendar'?calView():toolsView();
 h+='<div class="shell'+(S.tab==='deals'?'':' wide')+'">'+(S.tab==='deals'?rail():'')+main+'</div>'+sheet()+(S.notes?notesPanel():'')+(S.toast?'<div class="toast" role="status">'+esc(S.toast)+'</div>':'');
 document.getElementById('app').innerHTML=h;window.scrollTo(0,y)}
function say(t){S.toast=t;render();clearTimeout(say.t);say.t=setTimeout(function(){S.toast='';render()},3600)}
function go(h){if(location.hash==='#'+h)parse();else location.hash=h}
function parse(){var h=location.hash.slice(1);
 if(h==='price-list'){S.tab='tools';S.sub='rates'}
 else if(h==='portfolio-library'){S.tab='tools';S.sub='portfolio'}
 else if(h==='demo-tools'){S.tab='tools';S.sub='demo'}
 else if(h==='run-log'){S.tab='tools';S.sub='log'}
 else if(h==='calendar'||h==='tools'){S.tab=h;S.sub=null}
 else{S.tab='deals';if(h.indexOf('deal-')===0)S.deal=h.slice(5)}
 S.rail=false;S.prev=null;S.confirm=null;S.notes=false;render();if(S.tab==='deals')window.scrollTo(0,0)}
window.addEventListener('hashchange',parse);

/* ---------- actions ---------- */
function cur(){return DEALS[S.deal]}
function liveDraft(d){var t=document.getElementById('draft');if(t)S.drafts[d.id]=t.value;return draftText(d)}
async function approveFlow(d){
 var text=liveDraft(d);S.busy=true;render();
 var vid=d.v.version_id;
 if(text!==d.v.draft){var r=await api('/api/draft',{version_id:vid,body:text});if(!r.ok){S.busy=false;say('Not sent. '+(r.message||'The draft could not be saved.'));return}vid=r.version_id}
 var ids=ticked(d).map(function(o){return o.id});
 var c=await api('/api/check',{version_id:vid,option_ids:ids.length===d.opts.length?undefined:ids});
 await refreshState(false);d=DEALS[S.deal];
 S.busy=false;delete S.drafts[d.id];
 var vs=(c.result&&c.result.violations)||[];vs.forEach(function(x){x.message=x.message||c.message});
 S.check[d.id]=fromViolations(d,vs,!!c.ok);
 if(c.ok){S.confirm={deal:d.id,kind:'approve',vid:vid,ids:ids};render()}
 else{render();var k=S.check[d.id];say('Not sent. '+(k.safe.ok?(k.price.msg||c.message||'The check did not pass.'):k.safe.msg.replace('Blocked: ','')+'.'))}}
async function doApprove(d){
 var cf=S.confirm;S.busy=true;render();
 var r=await api('/api/approve',{version_id:cf.vid,option_ids:cf.ids.length===d.opts.length?undefined:cf.ids});
 S.busy=false;S.confirm=null;delete S.check[d.id];
 await refreshState(false);window.scrollTo(0,0);
 say(r.ok?'Proposal page created.':'Not sent. '+(r.message||'Approve was refused.'))}
document.addEventListener('click',async function(e){
 var b=e.target.closest('[data-act]');if(!b)return;var a=b.dataset.act,d=cur(),v,r;
 switch(a){
  case 'tab':S.sub=null;if(b.dataset.tab!=='calendar')S.hi=null;go(b.dataset.tab==='deals'?'deal-'+S.deal:b.dataset.tab);break;
  case 'open':S.hi=null;go('deal-'+b.dataset.id);break;
  case 'rail':S.rail=!S.rail;render();break;
  case 'notes':S.notes=!S.notes;render();break;
  case 'showTest':S.showTest=!S.showTest;render();break;
  case 'toggle':S.open[b.dataset.key]=!S.open[b.dataset.key];render();break;
  case 'more':S.more[d.id]=true;render();break;
  case 'tick':break;
  case 'preview':S.prev={deal:d.id,opt:b.dataset.opt,data:null};render();
   r=await api('/api/capacity-preview',{version_id:d.v.version_id,option_id:b.dataset.opt});
   if(S.prev&&S.prev.opt===b.dataset.opt){S.prev.data=r.ok?r:{ok:false,fits:false,error:r.message||'This option no longer fits your calendar.',weeks:[]};render()}break;
  case 'closePrev':S.prev=null;render();break;
  case 'sim':S.sim[d.id]=(S.sim[d.id]||0)+1;render();break;
  case 'leak':liveDraft(d);S.drafts[d.id]=draftText(d)+' Our minimum is '+money(d.est.lowest,1)+'.';delete S.check[d.id];render();break;
  case 'clean':S.drafts[d.id]=cleanDraft(d);delete S.check[d.id];render();break;
  case 'approve':
   if(d.phase==='info'){liveDraft(d);S.confirm={deal:d.id,kind:'approve'};render();break}
   approveFlow(d);break;
  case 'reject':S.confirm={deal:d.id,kind:'reject'};render();break;
  case 'cancel':S.confirm=null;render();break;
  case 'doApprove':
   if(d.phase==='info'){S.busy=true;r=await api('/api/send-question',{deal_id:d.id,question:draftText(d)});S.busy=false;S.confirm=null;delete S.drafts[d.id];await refreshState(false);say(r.ok?'Question sent to '+d.client+'. It will return to your queue when they reply.':(r.message||'The question was not sent.'));break}
   doApprove(d);break;
  case 'doReject':S.busy=true;r=await api('/api/reject',{version_id:d.v.version_id});S.busy=false;S.confirm=null;await refreshState(false);window.scrollTo(0,0);say(r.ok?'Rejected. Nothing was sent to the client.':(r.message||'Reject was refused.'));break;
  case 'copy':v=location.origin+d.v.client_url;try{navigator.clipboard.writeText(v).then(function(){say('Link copied.')},function(){say('Copy blocked here. Select the link above.')})}catch(x){say('Copy blocked here. Select the link above.')}break;
  case 'page':window.open(d.v.client_url,'_blank','noopener');break;
  case 'confirmBooking':S.busy=true;r=await api('/api/confirm-booking',{version_id:d.v.version_id});S.busy=false;await refreshState(false);say(r.ok?'Booking confirmed. The hours are set aside on your calendar.':(r.message||'Confirm was refused.'));break;
  case 'rates':go('price-list');break;
  case 'tool':v=b.dataset.tool;if(v==='rates')go('price-list');else if(v==='portfolio')go('portfolio-library');else if(v==='demo')go('demo-tools');else if(v==='log')go('run-log');else if(v==='proposals')location.href='/commerce';else if(v==='inquiry')window.open('/inquire','_blank','noopener');break;
  case 'pf':S.pf=b.dataset.f;render();break;
  case 'addWork':location.href='/portfolio';break;
  case 'play':window.open('/portfolio/media/'+encodeURIComponent(b.dataset.id),'_blank','noopener');break;
  case 'saveRates':v=(ST.price_list||[]).map(function(x){return {id:x.id,hours:parseFloat(document.getElementById('rh-'+x.id).value),price_sgd:parseFloat(document.getElementById('rp-'+x.id).value)}});
   r=await api('/api/price-list',{items:v});await refreshState(false);say(r.ok?'Price list saved. It applies to new estimates.':(r.message||'The price list was not saved.'));break;
  case 'rerun':S.busy=true;render();r=await api('/api/reevaluate',{deal_id:d.id,offer_id:d.raw.offers[d.raw.offers.length-1].offer_id});S.busy=false;delete S.check[d.id];await refreshState(false);say(r.ok?'Estimate re-run against your current calendar.':(r.message||'The estimate could not be re-run.'));break;
  case 'seecal':S.hi={weeks:((d.v&&d.v.capacity_changes)||[]).filter(function(x){return Math.abs(x.before-x.after)>0.05&&monday(x.week)<=d.ask.by}).map(function(x){return x.week}),deal:d.id};S.win=0;go('calendar');break;
  case 'runEst':
   var t=readTotals(d);if(!(t.h>0)||!(t.p>0)){say('Add at least one item from your price list.');break}
   v=(ST.price_list||[]).map(function(x){var el=document.getElementById('q-'+x.id);return {id:x.id,qty:el?(parseFloat(el.value)||0):0}}).filter(function(x){return x.qty>0});
   S.busy=true;render();r=await api('/api/price-list-estimate',{deal_id:d.id,items:v});S.busy=false;await refreshState(false);window.scrollTo(0,0);
   say(r.ok?'Estimate done from your price list. A draft is ready.':(r.message||'The estimate could not be made.'));break;
  case 'win':v=parseInt(b.dataset.d,10);S.win=v===0?0:S.win+v;render();break;
  case 'saveHrs':v=parseFloat(document.getElementById('hrs').value);if(!(v>0)||v>80){say('Enter hours between 1 and 80.');break}
   r=await api('/api/calendar/hours',{hours:v});await refreshState(false);say(r.ok?'Working hours saved. '+ORDER.filter(function(k){return DEALS[k].stale}).length+' proposal(s) now need a fresh estimate.':(r.message||'Not saved.'));break;
  case 'addBlock':
   var bw=document.getElementById('bw').value,bh=parseFloat(document.getElementById('bh').value),br=document.getElementById('br').value;
   if(!(bh>0)||bh>80){say('Enter hours between 1 and 80.');break}
   r=await api('/api/calendar/block',{week:bw,hours:bh,reason:br});await refreshState(false);
   say(r.ok?'Blocked '+n1(bh)+' h in the week of '+lab(monday(bw))+'. '+ORDER.filter(function(k){return DEALS[k].stale}).length+' proposal(s) now need a fresh estimate.':(r.message||'Not saved.'));break;
  case 'delBlock':r=await api('/api/calendar/remove',{id:b.dataset.id});await refreshState(false);if(!r.ok)say(r.message||'Not removed.');break;
  case 'importRun':var st=document.getElementById('run-status'),bundle;try{bundle=JSON.parse(document.getElementById('run-json').value)}catch(x){st.textContent='Enter valid run JSON.';break}r=await api('/api/import-run',bundle);st.textContent=r.ok?'Imported run.':'Import failed. '+(r.message||r.error||'');refresh();break;
  case 'resetSample':var rs=document.getElementById('reset-status');r=await api('/api/reset-sample',{confirm:document.getElementById('reset-confirm').value,request_id:crypto.randomUUID()});rs.textContent=r.ok?'Sample data reset.':'Reset refused. '+(r.message||r.error||'');refresh();break;
 }
});
document.addEventListener('change',function(e){var b=e.target.closest('[data-act="tick"]');if(b){var d=cur(),t=tickset(d);t[b.dataset.opt]=b.checked;delete S.check[d.id];render()}});
document.addEventListener('input',function(e){
 if(e.target.id==='draft'){var d=cur();S.drafts[d.id]=e.target.value;delete S.check[d.id];S.confirm=null;var p=document.getElementById('checkpanel');if(p)p.innerHTML=checks(d)}
 if(e.target.classList&&e.target.classList.contains('qty')&&e.target.id.indexOf('q-')===0){var d2=cur();S.qty[d2.id]=S.qty[d2.id]||{};S.qty[d2.id][e.target.id.slice(2)]=parseFloat(e.target.value)||0;var t=document.getElementById('esttotals');if(t)t.innerHTML=totalsHtml(readTotals(d2))}});
window.addEventListener('keydown',function(e){if(e.key==='Escape'&&(S.prev||S.notes)){S.prev=null;S.notes=false;render()}});
window.importFixture=async function(i){var f=FIXTURES[i];var r=await api('/api/import',{source:'hashmeta',source_id:f.source_id,raw_inquiry:f.raw,offer_request:f.offer_request});var el=document.getElementById('intake-msg');if(el)el.textContent=r.ok?(r.duplicate?'Duplicate source ID: resumed the existing deal.':'Imported '+f.source_id+' as a new deal.'):'Import failed: '+(r.message||r.error);refresh()};
render();
refresh().then(parse);
schedulePolling();
})();

"""


def page(badge, fixtures):
    fixtures_json = json.dumps(fixtures).replace("</", "<\\/")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>BidPilot founder dashboard</title>'
            '<link rel="preconnect" href="https://fonts.googleapis.com">'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&display=swap">'
            '<style>' + CSS + '</style></head><body>'
            '<h1 style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)">BidPilot founder dashboard</h1>'
            '<div id="app"><p>Loading deals…</p></div>'
            '<noscript>' + badge + '</noscript>'
            '<script>const FIXTURES = ' + fixtures_json + ';\n</script>'
            '<script>' + JS + '</script></body></html>')
