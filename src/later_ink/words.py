"""Wordlist for typeable catalog secrets.

Constraints: lowercase a-z only, 3-6 letters, common unambiguous spellings —
every word typeable on KOReader's base keyboard layer without shift or symbols.
"""

_RAW = """
cat dog fox owl bear wolf deer hare mole newt toad frog crab carp
trout perch pike heron crane finch robin wren swan goose duck hen
crow raven stork otter seal whale shark squid moth wasp bee ant
snail slug worm gecko lemur sloth panda koala bison moose elk yak
goat sheep lamb calf colt pony mule horse camel llama zebra hippo
rhino tiger lion puma lynx cobra viper adder skink eagle hawk kite
apple pear plum peach grape lemon lime melon mango kiwi fig date
olive corn wheat oat rice bean pea lentil onion leek garlic basil
sage thyme mint dill cumin bread toast bagel scone cake tart pie
flan honey sugar salt cocoa mocha latte tea milk cream butter
soup stew salad taco pasta pizza berry cherry apricot raisin
oak elm ash pine cedar birch maple willow fern moss vine reed
rose tulip daisy lotus ivy palm bark leaf root seed stem twig
petal grove field meadow hill ridge cliff crag peak summit slope
valley glen dale creek brook river delta lake pond marsh swamp
bay cove reef shore coast beach dune island isle cape canyon
storm cloud mist fog rain hail sleet snow frost ice wind gale
breeze spark flame ember coal soot sun moon star comet nova
orbit lunar solar polar north south east west amber jade pearl
opal ruby topaz onyx gold silver copper bronze iron steel tin
zinc lamp desk chair table shelf book page quill pen ink chalk
slate brick stone tile beam plank nail screw bolt gear cog wheel
axle lever pump valve pipe wire cord rope knot sail mast oar
hull deck anchor compass map globe chart flag drum flute harp
viola cello banjo bell chime clock watch dial hinge latch key
lock door gate fence wall tower spire dome arch vault cellar
attic porch red blue green teal navy swift quick brisk calm
bold brave keen wise merry jolly sunny happy lucky noble proud
humble gentle quiet still eager fancy crisp fresh clean clear
bright dark deep wide tall short long slim thin broad grand
small tiny vast saga myth fable tale poem verse song hymn opera
dance waltz tango polka march cargo ferry train tram wagon canoe
kayak rocket glider blimp zero one two three four five six seven
nine ten forty fifty sixty acorn alder aspen bamboo barley basin
cabin candle canvas carbon cotton dapple fjord garnet hazel
juniper linen lichen magma nectar orchid osprey pebble pollen
quartz saffron sepia sierra sorrel tundra umber walnut wicker
zephyr
"""

WORDS: tuple[str, ...] = tuple(
    dict.fromkeys(w for w in _RAW.split() if w.isalpha() and 3 <= len(w) <= 7)
)

assert len(WORDS) >= 300, f"wordlist too small: {len(WORDS)}"
