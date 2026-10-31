from pathlib import Path
import subprocess
from installer_runner import run_installer,InstallerExit
from silent_install import silent_command


def manual_command(installer):
    return ['msiexec','/i',str(installer)] if Path(installer).suffix.casefold()=='.msi' else [str(installer)]


def exit_explanation(code):
    return {1638:'Windows Installer reported another version of a product or prerequisite is already installed. Read the wizard to identify which one; do not remove unrelated runtimes.',
            1618:'Another Windows installation is running. Finish that installation before continuing.',
            1602:'The installation was cancelled.',
            5:'The installer could not complete. Its visible wizard may explain a file or installation error; code 5 alone does not identify the cause.',
            2:'The installer stopped before completion. Review the visible wizard for its exact error.'}.get(int(code),f'The installer exited with code {code}. Review its wizard for the exact error.')


def execute_setup(installer,*,silent,credentials,log,attention,verify=lambda:None,runner=None):

    runner=runner or run_installer
    installer=Path(installer)
    background_only=silent and installer.name.casefold()=='borisfx continuum 2026.5 adobe v19.5.6.exe'
    user_owned=False
    def execute(command,quiet):
        nonlocal user_owned
        result=runner(command,installer.parent,silent=quiet,credentials=credentials,log=log,on_attention=attention,
                      handoff_on_attention=True)
        if getattr(result,'handed_off',False):return result,None
        code=int(result)
        parent_code=getattr(result,'parent_code',code)
        if (background_only and parent_code in (0,1641,3010) and
                getattr(result,'child_codes',()) and all(c==2 for c in result.child_codes)):
            code=parent_code
            log('Continuum installer reported success. Verifying installed host files; helper exits do not start another setup.')
        user_owned=user_owned or bool(getattr(result,'assisted',False))
        if code not in (0,1641,3010):
            reason=exit_explanation(code)
            if 1638 in getattr(result,'child_codes',()) and int(result)!=1638:
                reason+=' A child installer also reported 1638. '+exit_explanation(1638)
            error=RuntimeError(reason)
            error.code=code
            error.assisted=bool(getattr(result,'assisted',False))
            raise error
        verified=verify()
        return InstallerExit(code, bool(getattr(result,'assisted',False) or not quiet),getattr(result,'child_codes',()),parent_code=parent_code),verified
    if not silent:
        attention('manual','Complete this installer wizard, then click Finish. File verification runs afterwards.')
        return execute(manual_command(installer),False)
    try:
        command,engine=silent_command(installer,credentials)
        log('Attempting silent setup: '+installer.name+' ('+engine+')')
        return execute(command,True)
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
        if background_only:
            log('BACKGROUND SETUP STOPPED: '+str(error)+'. No manual installer was launched.')
            raise
        if getattr(error,'retry_safe',True) is False:
            attention('visible','Setup tracking failed while an installer may still be running. Do not launch another setup. Complete or close its existing wizard. '+str(error))
            raise
        if getattr(error,'code',None)==1602:
            raise
        if user_owned or getattr(error,'assisted',False):
            raise


        log('SILENT SETUP DID NOT COMPLETE: '+str(error))
        log('Opening the normal installer once; follow the installer assistance window.')
        attention('retry',str(error))
        return execute(manual_command(installer),False)
