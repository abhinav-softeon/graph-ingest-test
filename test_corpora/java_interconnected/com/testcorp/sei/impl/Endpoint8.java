package com.testcorp.sei.impl;

import javax.jws.WebMethod;
import javax.jws.WebService;
import com.testcorp.api.Handler;
import com.testcorp.api.HandlerImpl8;
import com.testcorp.manager.Manager8;
import com.testcorp.facade.Facade0;
import com.testcorp.service.Service8;

@WebService
public class Endpoint8 {

    @WebMethod
    public String lookup(String id) throws Exception {
        return new Service8().handle(id);
    }

    @WebMethod
    public String dispatch(String id) throws Exception {
        Handler h = new HandlerImpl8();
        return h.run(id);
    }

    @WebMethod
    public String viaManager(String id) throws Exception {
        return new Manager8().process(id);
    }

    @WebMethod
    public String deepChain(String id) throws Exception {
        return new Manager8().deep(id);
    }

    @WebMethod
    public String deepest(String id) throws Exception {
        return new Facade0().orchestrate(id);
    }

    @WebMethod
    public String viaFacade(String id) throws Exception {
        return new Facade0().orchestrateDirect(id);
    }
}
