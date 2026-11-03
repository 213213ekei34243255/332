"""Interview languages: what the interviewer speaks and what the browser should listen for.

Christ Junior College hires teaching and non-teaching staff who are comfortable in different languages, so every
interview can run in English, Malayalam, Kannada, Tamil, Telugu or Hindi. The administrator sets a default per
position and (unless they switch it off) the candidate picks the language they are most at ease with.

Spoken fixed lines (greeting, goodbye, "give me an example") live here too, so they stay in the interview language
even when the AI model is slow or unavailable.
"""

DEFAULT = "en"

LANGUAGES = {
    "en": {
        "name": "English", "native": "English", "speech": "en-IN",
        "sample": "Hello {name}. This is a quick sound check. If you can hear me, you're all set.",
        "opening": "Hi {name}, I'm {persona}, an AI interviewer. To start, could you tell me a bit about yourself "
                   "and what drew you to this role?",
        "closing": "Thank you, {name}. That's everything I wanted to cover. The hiring team will review your recording "
                   "and be in touch. Take care!",
        "fallback": "Thanks for sharing that. Could you give me a specific example from your own experience?",
    },
    "ml": {
        "name": "Malayalam", "native": "മലയാളം", "speech": "ml-IN",
        "sample": "നമസ്കാരം {name}. ഇതൊരു ചെറിയ ശബ്ദ പരിശോധനയാണ്. ഞാൻ പറയുന്നത് നിങ്ങൾക്കു കേൾക്കാമെങ്കിൽ, എല്ലാം ശരിയാണ്.",
        "opening": "നമസ്കാരം {name}, ഞാൻ {persona}, ഒരു AI ഇന്റർവ്യൂവറാണ്. തുടങ്ങാൻ, നിങ്ങളെക്കുറിച്ചും ഈ തസ്തികയിൽ "
                   "നിങ്ങൾക്ക് താൽപ്പര്യം തോന്നിയത് എന്തുകൊണ്ടാണെന്നും അല്പം പറയാമോ?",
        "closing": "നന്ദി, {name}. എനിക്ക് ചോദിക്കാനുണ്ടായിരുന്നതെല്ലാം കഴിഞ്ഞു. നിയമന സംഘം നിങ്ങളുടെ റെക്കോർഡിംഗ് "
                   "പരിശോധിച്ച് നിങ്ങളെ ബന്ധപ്പെടും. ശ്രദ്ധിക്കണേ!",
        "fallback": "പങ്കുവച്ചതിന് നന്ദി. നിങ്ങളുടെ സ്വന്തം അനുഭവത്തിൽ നിന്ന് ഒരു പ്രത്യേക ഉദാഹരണം പറയാമോ?",
    },
    "kn": {
        "name": "Kannada", "native": "ಕನ್ನಡ", "speech": "kn-IN",
        "sample": "ನಮಸ್ಕಾರ {name}. ಇದು ಒಂದು ಸಣ್ಣ ಧ್ವನಿ ಪರೀಕ್ಷೆ. ನನ್ನ ಮಾತು ನಿಮಗೆ ಕೇಳಿಸಿದರೆ, ಎಲ್ಲವೂ ಸರಿಯಾಗಿದೆ.",
        "opening": "ನಮಸ್ಕಾರ {name}, ನಾನು {persona}, ಒಬ್ಬ AI ಸಂದರ್ಶಕ. ಪ್ರಾರಂಭಿಸಲು, ನಿಮ್ಮ ಬಗ್ಗೆ ಮತ್ತು ಈ ಹುದ್ದೆಯಲ್ಲಿ "
                   "ನಿಮಗೆ ಆಸಕ್ತಿ ಏಕೆ ಎಂದು ಸ್ವಲ್ಪ ಹೇಳಬಹುದೇ?",
        "closing": "ಧನ್ಯವಾದಗಳು, {name}. ನಾನು ಕೇಳಲು ಬಯಸಿದ್ದೆಲ್ಲವೂ ಮುಗಿದಿದೆ. ನೇಮಕಾತಿ ತಂಡ ನಿಮ್ಮ ರೆಕಾರ್ಡಿಂಗ್ ಪರಿಶೀಲಿಸಿ "
                   "ನಿಮ್ಮನ್ನು ಸಂಪರ್ಕಿಸುತ್ತದೆ. ಜಾಗರೂಕರಾಗಿರಿ!",
        "fallback": "ಹಂಚಿಕೊಂಡಿದ್ದಕ್ಕೆ ಧನ್ಯವಾದಗಳು. ನಿಮ್ಮ ಸ್ವಂತ ಅನುಭವದಿಂದ ಒಂದು ನಿರ್ದಿಷ್ಟ ಉದಾಹರಣೆ ನೀಡಬಹುದೇ?",
    },
    "ta": {
        "name": "Tamil", "native": "தமிழ்", "speech": "ta-IN",
        "sample": "வணக்கம் {name}. இது ஒரு சிறிய ஒலிச் சோதனை. நான் பேசுவது உங்களுக்குக் கேட்டால், எல்லாம் சரியாக உள்ளது.",
        "opening": "வணக்கம் {name}, நான் {persona}, ஒரு AI நேர்காணலர். தொடங்குவதற்கு, உங்களைப் பற்றியும் இந்தப் பணியில் "
                   "உங்களுக்கு ஆர்வம் ஏன் என்றும் சற்றுச் சொல்ல முடியுமா?",
        "closing": "நன்றி, {name}. நான் கேட்க நினைத்த அனைத்தும் முடிந்தது. தேர்வுக் குழு உங்கள் பதிவைப் பார்த்து "
                   "உங்களைத் தொடர்பு கொள்ளும். கவனமாக இருங்கள்!",
        "fallback": "பகிர்ந்ததற்கு நன்றி. உங்கள் சொந்த அனுபவத்திலிருந்து ஒரு குறிப்பிட்ட உதாரணம் சொல்ல முடியுமா?",
    },
    "te": {
        "name": "Telugu", "native": "తెలుగు", "speech": "te-IN",
        "sample": "నమస్కారం {name}. ఇది ఒక చిన్న సౌండ్ చెక్. నా మాట మీకు వినిపిస్తే, అంతా సరిగ్గా ఉంది.",
        "opening": "నమస్కారం {name}, నేను {persona}, ఒక AI ఇంటర్వ్యూయర్‌ని. ప్రారంభించడానికి, మీ గురించి మరియు ఈ పదవిపై "
                   "మీకు ఆసక్తి ఎందుకో కొంచెం చెప్పగలరా?",
        "closing": "ధన్యవాదాలు, {name}. నేను అడగాలనుకున్నవన్నీ పూర్తయ్యాయి. నియామక బృందం మీ రికార్డింగ్‌ను పరిశీలించి "
                   "మిమ్మల్ని సంప్రదిస్తుంది. జాగ్రత్తగా ఉండండి!",
        "fallback": "పంచుకున్నందుకు ధన్యవాదాలు. మీ స్వంత అనుభవం నుండి ఒక నిర్దిష్టమైన ఉదాహరణ చెప్పగలరా?",
    },
    "hi": {
        "name": "Hindi", "native": "हिन्दी", "speech": "hi-IN",
        "sample": "नमस्ते {name}। यह एक छोटा साउंड चेक है। अगर आप मुझे सुन पा रहे हैं, तो सब ठीक है।",
        "opening": "नमस्ते {name}, मैं {persona} हूँ, एक AI इंटरव्यूअर। शुरू करने के लिए, क्या आप अपने बारे में थोड़ा बता "
                   "सकते हैं और इस पद में आपकी रुचि क्यों है?",
        "closing": "धन्यवाद, {name}। जो कुछ पूछना था, वह पूरा हो गया। भर्ती टीम आपकी रिकॉर्डिंग देखकर आपसे संपर्क करेगी। "
                   "अपना ध्यान रखिए!",
        "fallback": "यह साझा करने के लिए धन्यवाद। क्या आप अपने अनुभव से कोई ख़ास उदाहरण बता सकते हैं?",
    },
}

_ALIASES = {}
for _code, _info in LANGUAGES.items():
    for _key in (_code, _info["name"], _info["native"], _info["speech"]):
        _ALIASES[_key.strip().lower()] = _code
    _ALIASES[_info["speech"].split("-")[0].lower()] = _code


def resolve(value, default=DEFAULT):
    """Language code for a code, English name, native name or BCP-47 locale ('Malayalam', 'ml', 'ml-IN', 'മലയാളം')."""
    key = str(value or "").strip().lower().replace("_", "-")
    return _ALIASES.get(key) or _ALIASES.get(key.split("-")[0]) or default


def info(value):
    code = resolve(value)
    return dict(LANGUAGES[code], code=code)


def settings_for(value):
    """The language facts the interview settings carry (stored with the interview so it is stable on resume)."""
    i = info(value)
    return {"language": i["name"], "language_code": i["code"], "language_native": i["native"], "speech_lang": i["speech"]}


def line(value, key, **fmt):
    """A fixed spoken line ('opening', 'closing', 'fallback', 'sample') in the given language."""
    return LANGUAGES[resolve(value)][key].format(**fmt)


def public_list():
    """What the interview page needs to render the picker and play the sound-check sentence."""
    return [{"code": c, "name": i["name"], "native": i["native"], "speech_lang": i["speech"], "sample": i["sample"]}
            for c, i in LANGUAGES.items()]


def choose(position_language, requested, allow_choice=True):
    """The language an interview runs in: the candidate's pick when allowed, otherwise the position's default."""
    base = resolve(position_language)
    if allow_choice and requested:
        return resolve(requested, base)
    return base


def is_known(value):
    key = str(value or "").strip().lower().replace("_", "-")
    return key in _ALIASES or key.split("-")[0] in _ALIASES
