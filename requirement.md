INNATE	AI	LTD	·	CONTRACT	DEVELOPER	HIRING
Test brief: storefront capture & visualisation
Stage	1	of	3
THE CLIENT AND THE PRODUCT
Our	client	supplies	design-led	outdoor	planters	to	restaurants,	salons	and	offices	in	London,	on	a	
purchase	or	monthly	rental	basis	with	seasonal	replanting.	We	are	building	them	a	prospecting	engine.	
Before	any	outreach	happens,	the	client	wants	to	identify	independent	venues	around	London	— cafes,	
restaurants,	salons,	and	similar	street-facing	businesses	— with	bare	or	under-dressed	frontages,	where	
the	client’s	planters	would	visibly	enhance	the	entrance.	For	each	one,	the	system	needs	to	produce	a	
realistic	visual	of	that	venue’s	own	doorway,	dressed	with	the	client’s	real	products,	convincing	enough	
to	send	to	the	owner.
You	are	being	tested	on	the	two	capabilities	at	the	technical	heart	of	that:	getting	a	usable,	well-framed	
photo	of	a	real	venue’s	frontage	(real	street	view),	and	compositing	the	client’s	actual	planters	onto	it	
believably	and	at	correct	scale.	You’re	building	the	part	that	turns	a	venue	into	a	convincing	“what	it	
could	look	like.”
For	a	sense	of	the	ambition	here	(not	the	exact	build):	
https://x.com/everestchris6/status/2043061132911677924 — a	similar	concept	applied	to	backyard	
pools,	sourced	and	rendered	fully	automatically.	We’re	not	replicating	that	build,	but	it’s	a	useful	
reference	point	for	the	level	of	polish	and	realism	we	want	the	output	to	have.
You	are	expected	to	use	AI	coding	tools	throughout.	We	are	assessing	your	decisions,	not	your	typing,	
and	every	decision	in	your	submission	must	be	one	you	can	defend	on	a	call	without	notes.
WHAT YOU WILL BUILD
Your	submission	has	three	parts.
1. A	short	design	note,	design.md,	covering	both	capabilities	below	at	a	depth	a	second	engineer	
could	build	from.
2. A	working	prototype,	built	end	to	end	on	the	three	venues	you	choose,	that	takes	a	venue	and	
outputs	a	composited	“planters	installed”	visual.
WHAT YOUR PROTOTYPE NEEDS TO DO
Getting	the	frontage	image. From	a	venue’s	location,	produce	a	well-framed	image	of	its	actual	
entrance	— not	a	generic	stock	photo,	the	real	doorway.	Your	design	note	should	cover:	how	you’d	
derive	a	usable	framing	from	a	location	and,	where	relevant,	panorama	data	(camera	heading,	field	of	
view);	what	you	do	when	the	nearest	available	imagery	faces	the	wrong	way	or	doesn’t	clearly	show	the	
entrance;	and	your	written	position	on	the	imagery-rights	question	this	raises	— you’re	capturing	and	
reusing	a	photo	of	someone’s	real	property	at	commercial	scale,	without	their	involvement	at	this	stage.
Compositing	the	planters. Take	the	client’s	real	product	photography	and	place	it	into	the	frontage	
image	so	it	looks	like	it’s	actually	there	— correct	scale,	correct	perspective,	believable	shadow	and	
grounding,	the	building	and	everything	else	in	the	shot	left	unaltered.	This	must	use	the	client’s	actual	
products	— three	reference	photos	are	provided	below	— not	a	generic	AI	approximation	of	“some	
planters.”
Your	design	note	should	cover:	how	you’d	estimate	real-world	scale	from	a	reference	object	in	the	
photo;	how	you’d	keep	the	products	visually	faithful	to	the	reference	photos	rather	than	letting	the	
model	reinterpret	them;	and	your	rejection	criteria	— what	makes	a	generation	bad	enough	that	it	
should	never	reach	a	venue	owner.
CHOOSING YOUR VENUES
Rather	than	working	from	a	fixed	list,	your	prototype	needs	to	find	roughly	three	or	more	candidate	
venues	in	London	itself	— independent	cafes,	restaurants,	salons,	or	similar	street-facing	businesses	
with	a	bare	or	under-dressed	frontage	where	the	client’s	planters	would	visibly	improve	the	entrance.	
This	selection	step	should	be	automated,	not	something	you	curate	by	hand:	pull	a	candidate	list	from	a	
real	source	(the	Google	Places	API,	OpenStreetMap,	or	another	source	of	your	choice),	then	have	your	
own	code	— AI-assisted	or	not	— decide	which	ones	actually	look	like	good	fits,	using	whatever	signals	
you	can	reasonably	extract	(business	type,	or	a	vision-model	judgement	on	the	frontage	itself,	for	
instance).
In	design.md,	list	the	venues	your	prototype	selected	— name,	address,	postcode	— and	explain	the	
logic	(or	the	model	prompt/criteria)	that	picked	them,	along	with	anything	it	rejected	and	why.	At	
5,000+	venues	a	week,	nothing	in	this	pipeline	can	rely	on	someone	manually	eyeballing	a	good	
candidate	or	the	best	framing	of	its	doorway	— the	same	applies	to	the	frontage-image	capture	that	
follows.	In	practice	that	means	your	own	code	needs	to	decide,	unaided,	whether	a	captured	image	is	
actually	usable,	and	if	Street	View	coverage	is	poor	or	faces	the	wrong	way,	fall	back	to	another	source	
— the	venue’s	own	website	or	Google	Business	photos	are	a	reasonable	choice.	State	your	own	
accept/reject	bar	for	what	makes	a	candidate	venue	or	a	framing	usable,	and	don’t	over-invest	in	polish	
here:	one	or	two	rejected	attempts	before	falling	back	is	a	perfectly	good	outcome	— we’re	grading	that	
the	decision-making	is	automated,	not	that	it’s	flawless.
DELIVERABLES
One	GitHub	repo	containing	design.md,	a	working	link	of	your	prototype	(deployed	on	Vercel,	html,	
etc.) and	README.md.	Attach the	repo	link.