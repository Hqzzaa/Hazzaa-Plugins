from installer_automation import normalized_label


def advance_bcc_license_page(forms,window,completed):

    title=forms.text(window).casefold()
    if 'boris' not in title or 'license' not in title:return False
    controls=forms.controls(window)
    text=' '.join(c.text for c in controls).casefold()
    if 'continuum' not in text or 'not licensed' not in text:return False
    radios=[c for c in controls if 'button' in c.class_name.casefold()
            and normalized_label(c.text)=='completeinstallationwithoutenteringalicense']
    next_buttons=[c for c in controls if 'button' in c.class_name.casefold()
                  and normalized_label(c.text)=='next']
    if len(radios)!=1 or len(next_buttons)!=1:return False
    signature=(window,radios[0].handle,next_buttons[0].handle)
    if signature in completed:return False
    forms.send(radios[0].handle,0x00F5)
    if forms.send(radios[0].handle,0x00F0)!=1:return False
    completed.add(signature)
    forms.send(next_buttons[0].handle,0x00F5)
    return True


def requires_input(forms,window):

    controls=forms.controls(window)
    if any(c.is_edit and not c.text for c in controls):return True
    buttons=[c for c in controls if 'button' in c.class_name.casefold()]
    ignored={'cancel','back','help','hide','minimize',''}
    actionable=[c for c in buttons if normalized_label(c.text) not in ignored]
    if actionable:return True
    has_progress=any('progress' in c.class_name.casefold() for c in controls)

    return not has_progress and not buttons


def input_grace(forms,window):
    controls=forms.controls(window)
    if any(c.is_edit or ('button' in c.class_name.casefold() and
            normalized_label(c.text) not in {'cancel','back','help','hide','minimize',''}) for c in controls):
        return 2
    return 8
