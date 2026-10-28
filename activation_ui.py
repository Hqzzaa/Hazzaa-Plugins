import tkinter as tk
from activation_details import activation_only

FONT=('Courier New',11,'bold')


class ActivationWindow:
    def __init__(self,app):
        self.app=app
        self.root=tk.Toplevel(app.root)
        self.root.title('HAZZAA ACTIVATION DETAILS')
        self.root.geometry('900x430')
        self.root.minsize(780,360)
        self.root.configure(bg='black')
        self.root.transient(app.root)
        self.product_label=tk.Label(self.root,bg='black',fg='white',font=FONT,anchor='w',wraplength=900)
        self.product_label.pack(fill='x',padx=18,pady=(16,8))
        body=tk.Frame(self.root,bg='black');body.pack(fill='both',expand=True,padx=18)
        self.products=tk.Listbox(body,bg='#151515',fg='white',selectbackground='#444444',
            exportselection=False,font=FONT,relief='flat',width=27)
        self.products.pack(side='left',fill='y',padx=(0,14))
        self.products.bind('<<ListboxSelect>>',lambda _:self.select_product())
        detail=tk.Frame(body,bg='black');detail.pack(side='left',fill='both',expand=True)
        self.instructions=tk.Label(detail,bg='black',fg='white',font=FONT,wraplength=590,justify='left',anchor='w')
        self.instructions.pack(fill='x',pady=(0,10))
        self.document_var=tk.StringVar(master=self.root)
        self.document_menu=tk.OptionMenu(detail,self.document_var,'',command=lambda _:self.show_document())
        self.document_menu.configure(bg='#202020',fg='white',activebackground='#444444',activeforeground='white',font=FONT,relief='flat',highlightthickness=0)
        self.document_menu['menu'].configure(bg='#202020',fg='white',font=FONT)
        self.document_menu.pack(fill='x')
        self.fields=tk.Frame(detail,bg='black');self.fields.pack(fill='x',pady=8)
        footer=tk.Frame(self.root,bg='black');footer.pack(fill='x',padx=18,pady=12)
        app._button(footer,'COPY ALL FIELDS',lambda:self.copy(self.current_document.get('text',''))).pack(side='left')
        app._button(footer,'CLOSE',self.root.destroy).pack(side='right')
        self.current_document={}
        self.update_records()
        app._apply_cursor(self.root);self.root.update_idletasks();app._set_dark_titlebar(self.root)

    def update_records(self,selected_id=None):
        self.records=sorted((clean for record in self.app.activation_records.values()
                            if (clean:=activation_only(record))),key=lambda r:r['product'].casefold())
        self.products.delete(0,'end')
        for record in self.records:self.products.insert('end',record['product'])
        if self.records:
            index=next((i for i,r in enumerate(self.records) if r['id']==selected_id),0)
            self.products.selection_set(index);self.products.see(index);self.select_product()
        else:
            self.product_label.configure(text='NO ACTIVATION DETAILS SAVED')
            self.instructions.configure(text='Only products with a code to enter inside After Effects appear here.')
            self.current_document={}
            self.document_menu.pack_forget()
            for child in self.fields.winfo_children():child.destroy()

    def select_product(self):
        selected=self.products.curselection()
        if not selected:return
        record=self.records[selected[0]]
        self.current_record=record
        self.product_label.configure(text='PLUGIN: '+record['product'])
        message='Open this product in After Effects, open Register / Options, and paste the matching code below.'
        if record.get('plugins'):message+='\nFILES: '+', '.join(record['plugins'])
        self.instructions.configure(text=message)
        menu=self.document_menu['menu'];menu.delete(0,'end')
        if len(record['documents'])>1:
            self.document_menu.pack(fill='x',before=self.fields)
        else:
            self.document_menu.pack_forget()
        for doc in record['documents']:
            menu.add_command(label=doc['name'],command=lambda name=doc['name']:(self.document_var.set(name),self.show_document()))
        if record['documents']:
            preferred=next((doc for doc in record['documents']
                if any(key=='serial' or key.startswith('code ') for key in doc.get('values',{}))),
                next((doc for doc in record['documents'] if doc.get('values')),record['documents'][0]))
            self.document_var.set(preferred['name']);self.show_document()

    def show_document(self):
        doc=next((d for d in self.current_record['documents'] if d['name']==self.document_var.get()),{})
        self.current_document=doc
        for child in self.fields.winfo_children():child.destroy()
        for key,value in doc.get('values',{}).items():
            row=tk.Frame(self.fields,bg='black');row.pack(fill='x',pady=3)
            tk.Label(row,text=key.upper(),bg='black',fg='white',font=FONT).pack(side='left',padx=(0,8))
            field=tk.Entry(row,bg='#202020',fg='white',readonlybackground='#202020',font=FONT,relief='flat')
            field.insert(0,value);field.configure(state='readonly');field.pack(side='left',fill='x',expand=True)
            self.app._button(row,'COPY',lambda value=value:self.copy(value)).pack(side='right',padx=(8,0))
        self.app._apply_cursor(self.fields)

    def copy(self,value):
        self.root.clipboard_clear();self.root.clipboard_append(value)
