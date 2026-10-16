package kit

import "testing"

var digestVectors = []struct{ in, sha string }{
	{"{\"b\":1,\"a\":[true,null,\"x\"]}", "54a65415ad370228851a1da4b31b6fd42dc58b19a50d35cae759325f7388ce64"},
	{"{\"z\":{\"y\":1.5,\"x\":100},\"é\":\"ü\",\"€\":\"€\"}", "8e4a2669ffe480b94fadf54774c1cef58c88f47c939a5faea01e4910f4351a84"},
	{"[1e21,1e-7,0.000001,0.1,5e-324,1.7976931348623157e308,-0,1.0,1e3,1.5,0.00001,123.456]", "fd2ccc86e5937842fbf30fe2376f3695a57c8105febb9f474db883b1d14d5ead"},
	{"{\"s\":\"a\\\"b\\\\c\\n\\t\\u0001\\u007f\\u2028\"}", "c96c22c94988f2e64116e7419b38fc414ef4b5b3ed127c9c2011de81627e6e52"},
	{"{\"😀\":1,\"￿\":2,\"a\":3}", "92a98ee3f7f8f30bd80c9d464a68ad47d46e0da2bc7da9c2b5a391e15fd1b4ff"},
	{"9007199254740991", "f40b423c2dd95ff2b2f027e22208f438cf7242862e5e746860e697308c9add26"},
	{"-9007199254740991", "4c933a456bb8f2e9894b2d0b26480439cd72d52fb8a27cbf8fd7f323f22c7815"},
	{"[]", "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"},
	{"\"x\"", "ba2df4903a2c14e86dc3bcca58911b44ac1d2514b7227bf6eb08cfb978f55a1b"},
	{"{}", "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"},
	{"[[],{},[{}]]", "c5364ec3a0556c645c7ee4367a71e408ee41c3e7d87dde75720e406d1a597ab9"},
}

var digestRefused = []string{
	"9007199254740992",
	"100000000000000000000",
	"{\"a\":1,\"a\":2}",
	"{\"a\":NaN}",
	"[1,]",
	"1 2",
	"",
	"1e999",
}

func TestDigestMatchesPythonZeocore(t *testing.T) {
	for _, v := range digestVectors {
		got, err := Digest([]byte(v.in))
		if err != nil {
			t.Fatalf("Digest(%s): %v", v.in, err)
		}
		if got != v.sha {
			t.Errorf("Digest(%s) = %s, want %s (from `zeocore digest`)", v.in, got, v.sha)
		}
	}
}

func TestDigestRefusesWhatPythonRefuses(t *testing.T) {
	for _, in := range digestRefused {
		if _, err := Digest([]byte(in)); err == nil {
			t.Errorf("Digest(%q) accepted; `zeocore digest` refuses it", in)
		}
	}
}
