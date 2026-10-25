import type { Activity } from '../types'

type Props = { activities: Activity[]; selected: string; zoom: number; layers: boolean; onSelect: (id: string) => void }
const lake = 'M760 215C809 224 835 253 892 250C935 246 970 230 1012 252C1042 277 1027 327 1052 363C1098 400 1106 447 1074 490C1061 533 1020 545 1003 590C992 626 955 667 908 679C861 688 837 669 805 635C779 608 750 593 743 552C729 507 740 480 725 442C713 400 737 369 729 333C712 291 714 241 760 215Z'
const roads = [
  'M80 15C230 180 260 130 360 250S530 320 610 410S580 600 645 720S760 850 780 950',
  'M280 0C200 180 330 200 420 340S345 590 410 670S530 850 500 950',
  'M0 170C210 185 400 50 550 125S760 105 850 100S1210 170 1400 110',
  'M0 490C180 400 250 440 400 410S560 350 690 300',
  'M0 730C180 650 350 710 470 620S615 540 720 540',
  'M500 0C510 115 575 160 655 230S700 370 702 490S635 660 700 760S880 850 940 950',
  'M1130-50C1100 160 1125 330 1135 490S1090 730 1130 950',
  'M1230-50C1210 150 1215 340 1240 510S1190 750 1220 950',
  'M1330-50C1295 160 1355 360 1320 540S1290 750 1320 950',
  'M690 180C765 203 780 270 838 275S968 227 1015 262S1025 343 1070 390S1082 465 1062 510',
  'M580 800C715 718 825 735 915 742S1060 683 1140 696S1310 760 1450 704',
  'M930-50C965 80 985 141 1050 170S1230 213 1400 193',
  'M1060 300C1145 270 1230 292 1410 260', 'M1090 420C1170 390 1255 424 1400 401',
  'M1070 540C1185 505 1290 560 1410 536', 'M1010 635C1140 599 1240 650 1420 630',
]
const parks = ['M0 0H700L710 140 610 195 480 120 395 260 220 250 100 390 0 350Z', 'M0 400L210 370 390 470 480 370 620 440 640 600 545 735 400 650 260 750 0 690Z', 'M0 760L250 720 400 815 560 730 740 810 880 900H0Z', 'M660 55L770 45 850 140 900 200 790 215 690 175Z', 'M655 345L710 340 705 515 650 550 590 465Z', 'M750 665L845 700 885 775 790 820 690 735Z']

export function MapCanvas({ activities, selected, zoom, layers, onSelect }: Props) {
  return <div className={`map-canvas ${layers ? '' : 'map-canvas--quiet'}`}>
    <svg className="map-art" viewBox="0 0 1400 900" preserveAspectRatio="xMidYMid slice" aria-label="杭州西湖示意地图，地点可点击" style={{ transform: `scale(${zoom})` }}>
      <defs>
        <pattern id="city-blocks" width="62" height="53" patternUnits="userSpaceOnUse" patternTransform="rotate(-8)">
          <rect width="62" height="53" fill="#edf3f2" /><rect x="5" y="5" width="23" height="17" rx="2" fill="#e0ebe9" /><rect x="33" y="5" width="24" height="37" rx="2" fill="#e2eae7" /><rect x="5" y="27" width="23" height="15" rx="2" fill="#dbe8e6" /><path d="M0 49H62M0 0V53" stroke="#fff" strokeWidth="5" />
        </pattern>
        <linearGradient id="lake-water" x1="0" x2="1" y1="0" y2="1"><stop stopColor="#9dd4e7" /><stop offset="1" stopColor="#b8deeb" /></linearGradient>
        <linearGradient id="land" x1="0" x2="1" y1="0" y2="1"><stop stopColor="#deeee5" /><stop offset="1" stopColor="#edf6ee" /></linearGradient>
      </defs>
      <rect width="1400" height="900" fill="url(#land)" />
      <path d="M970 0H1400V900H1080L1050 700 1100 440 1020 260Z" fill="url(#city-blocks)" />
      <g className="map-parks" fill="#cae5d4">{parks.map((d, i) => <path d={d} key={i} />)}</g>
      <g className="map-contours" fill="none" stroke="#bedbc9" strokeWidth="2" opacity=".48">{Array.from({ length: 17 }, (_, i) => <path key={i} d={`M${-110+i*28} 40C${55+i*20} ${110+i*8} ${45+i*24} ${220+i*13} ${145+i*28} ${320+i*12}S${120+i*32} ${570+i*4} ${225+i*27} ${730+i*4}`} />)}</g>
      <path d="M1280 0C1240 160 1290 310 1260 440S1260 720 1230 900" stroke="#c5e3ec" strokeWidth="20" fill="none" />
      <g stroke="#fff" strokeWidth="7" fill="none" strokeLinecap="round">{roads.map((d, i) => <path d={d} key={i} />)}</g>
      <g stroke="#f9fdfc" strokeWidth="3" fill="none" opacity=".9">{Array.from({ length: 21 }, (_, i) => { const x = 1050+(i%7)*55; const y = Math.floor(i/7)*290+30; return <path key={i} d={`M${x} ${y}l-22 130 45 70-10 120M${x-20} ${y+70}l150-10`} /> })}<path d="M560 145L590 230 550 330 590 440 510 525 550 620M370 75L460 170 430 240 520 305M270 580L355 540 400 600 490 560M30 350L160 315 240 390 325 340M590 790L660 825 680 900" /></g>
      <path d={lake} fill="url(#lake-water)" stroke="#eaf6e8" strokeWidth="9" />
      <path d="M750 230C760 300 746 365 751 420S780 510 770 570" stroke="#d7e7c6" strokeWidth="10" fill="none" /><path d="M750 230C760 300 746 365 751 420S780 510 770 570" stroke="#f8f6df" strokeWidth="3" fill="none" />
      <path d="M780 252L828 286 922 286 1006 255" stroke="#d0dfc5" strokeWidth="10" fill="none" /><path d="M780 252L828 286 922 286 1006 255" stroke="#fff9e9" strokeWidth="3" fill="none" />
      <g fill="#bfddc8" stroke="#dcedd9" strokeWidth="5"><path d="M859 451l21-14 16 13-1 19-24 8-14-13Z" /><path d="M883 544l25-9 15 18-7 20-25 3-13-16Z" /><path d="M950 376l14-10 19 9-7 18-20 3Z" /></g>
      <g fill="none" stroke="#eaf7fb" strokeWidth="1.5" opacity=".65"><path d="M790 400C880 425 941 440 1053 465" strokeDasharray="5 6" /><path d="M1003 285C970 365 975 437 913 547" strokeDasharray="5 6" /></g>
      <text x="900" y="470" textAnchor="middle" className="lake-label">西 湖</text><text x="902" y="497" textAnchor="middle" className="lake-label-en">WEST LAKE</text>
      {layers && <g className="map-labels"><text x="620" y="110">西溪国家湿地公园</text><text x="855" y="174">宝石山</text><text x="439" y="325">灵隐寺</text><text x="597" y="395">杭州植物园</text><text x="572" y="640">龙井村</text><text x="720" y="614">花港观鱼</text><text x="850" y="728">雷峰塔</text><text x="920" y="580">三潭映月</text><text x="1210" y="303">杭州站</text><text x="1070" y="100">西湖文化广场</text><text x="888" y="222" transform="rotate(-9 888 222)">北山街</text><text x="1110" y="410" transform="rotate(86 1110 410)">延安路</text><text x="947" y="746" transform="rotate(13 947 746)">南山路</text><text x="754" y="480" transform="rotate(88 754 480)">苏堤</text></g>}
      {activities.length > 1 && <g fill="none" strokeLinecap="round" strokeLinejoin="round"><polyline points={activities.map(activity => `${activity.x},${activity.y}`).join(' ')} stroke="#f8ffff" strokeWidth="9" /><polyline points={activities.map(activity => `${activity.x},${activity.y}`).join(' ')} stroke="#20695e" strokeWidth="4" strokeDasharray="8 5" /></g>}
      {activities.map((activity, index) => <g key={activity.id} transform={`translate(${activity.x} ${activity.y})`} className={`map-marker ${activity.id === selected ? 'is-selected' : ''}`} role="button" tabIndex={0} aria-label={`查看${activity.title}`} onClick={() => onSelect(activity.id)} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect(activity.id) } }}>
        <circle cy="-17" r="30" className="marker-halo" /><path d="M0 7C-7-4-18-13-18-24a18 18 0 0 1 36 0C18-13 7-4 0 7Z" fill={activity.id === selected ? '#1457d9' : '#175f56'} stroke="white" strokeWidth="3" /><text y="-18" textAnchor="middle" className="marker-number">{index + 1}</text><rect x="26" y="-37" width={activity.title.length*16+20} height="30" rx="8" fill="#ffffffed" /><text x="36" y="-16" className="marker-label">{activity.title}</text>
      </g>)}
    </svg>
    <div className="map-wash" />
  </div>
}
